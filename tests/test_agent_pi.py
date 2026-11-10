"""Replayed-stream tests: every AgentResult status without spawning pi, plus one real call."""

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import uuid
from io import BytesIO
from pathlib import Path
from subprocess import TimeoutExpired

import pytest

from lime import agent_pi, agents
from lime.agents import extract_envelope
from lime import config
from lime.data_types import AgentRequest, GenericOutput, ReviewOutput

MODEL = "openrouter/deepseek/deepseek-v4-flash"

# Runs a lime TypeScript extension for real: needs a node with built-in type stripping.
needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH (runs a lime TypeScript extension for real)"
)


def make_request(tmp_path, **overrides) -> AgentRequest:
    fields: dict = dict(
        runtime="pi",
        model=MODEL,
        system_prompt="You are a test agent.",
        prompt="Reply with exactly the word: pong",
        session_id=str(uuid.uuid4()),
        session_dir=str(tmp_path / "session"),
        raw_output_path=str(tmp_path / "raw_output.jsonl"),
        cwd=str(tmp_path),
    )
    fields.update(overrides)
    return AgentRequest(**fields)


def assistant_message_end(text="pong", input=1552, cache_read=0, cache_write=0):
    return {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "deepseek/deepseek-v4-flash",
            "usage": {
                "input": input,
                "output": 3,
                "cacheRead": cache_read,
                "cacheWrite": cache_write,
                "reasoning": 0,
                "totalTokens": input + cache_read + cache_write + 3,
                "cost": {"input": 1e-4, "output": 5e-7, "cacheRead": 0, "cacheWrite": 0, "total": 1.05e-4},
            },
        },
    }


def ok_events(session_id="s-1"):
    return [
        {"type": "session", "id": session_id, "version": 3},
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_end", "message": {"role": "user", "content": []}},
        assistant_message_end(),
        {"type": "turn_end"},
        {"type": "agent_end"},
        {"type": "agent_settled"},
    ]


def tool_turn(text, n_tools):
    """One pi turn: the model answers with n tool calls, pi runs them, turn ends."""
    start = {"type": "tool_execution_start", "toolName": "ls"}
    end = {"type": "tool_execution_end", "toolName": "ls", "result": "ok"}
    return [{"type": "turn_start"}, assistant_message_end(text), *([start] * n_tools), *([end] * n_tools), {"type": "turn_end"}]


def to_lines(events):
    return [json.dumps(e).encode("utf-8") + b"\n" for e in events]


SUBMIT_ARGS = {"status": "success", "summary": "done", "artifacts": [], "notes_for_next_agent": ""}


def submit_start(call_id, args):
    return {"type": "tool_execution_start", "toolCallId": call_id, "toolName": "submit", "args": args}


def submit_end(call_id, ok=True, reason=""):
    result = {"terminate": True} if ok else {"content": [{"type": "text", "text": reason}], "details": {}}
    end = {"type": "tool_execution_end", "toolCallId": call_id, "toolName": "submit", "result": result}
    if not ok:
        end["isError"] = True
    return end


def submit_events(*calls):
    """One submit call per (start, end) pair, appended after the standard ok preamble."""
    base = ok_events()[:4] + [assistant_message_end(text="")]
    entries = []
    for call_id, args, ok, reason in calls:
        entries.append(submit_start(call_id, args))
        entries.append(submit_end(call_id, ok=ok, reason=reason))
    return base + entries + [
        {"type": "message_end", "message": {"role": "toolResult", "content": []}},
        {"type": "turn_end"},
        {"type": "agent_end"},
        {"type": "agent_settled"},
    ]


_LADDER = [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]


class FakeProc:
    """Stands in for Popen: replays a stream, optionally hangs until signalled."""

    def __init__(self, lines, returncode=0, hang=False, dies_on=signal.SIGINT, stderr=b""):
        self.pid = 4242
        self.signals: list[int] = []
        self.returncode = None
        self._rc = returncode
        self._dies_on = dies_on
        self._dead = threading.Event()
        if not hang:
            self._dead.set()
        self.stderr = BytesIO(stderr)
        self.stdout = self._stream(lines, hang)

    def _stream(self, lines, hang):
        yield from lines
        if hang:
            self._dead.wait()

    def send_signal(self, sig):
        self.signals.append(sig)
        if _LADDER.index(sig) >= _LADDER.index(self._dies_on):
            self._dead.set()

    def kill(self):
        self.send_signal(signal.SIGKILL)

    def wait(self, timeout=None):
        if not self._dead.wait(timeout):
            raise TimeoutExpired(cmd="pi", timeout=timeout or 0)
        self.returncode = self._rc
        return self._rc


@pytest.fixture
def fake_pi(monkeypatch):
    """Replaces Popen; returns a setter for the FakeProc the next run() gets."""
    holder = {}

    def popen(cmd, **kwargs):
        holder["cmd"], holder["kwargs"] = cmd, kwargs
        return holder["proc"]

    monkeypatch.setattr(agent_pi, "Popen", popen)
    monkeypatch.setattr(agent_pi, "SIGINT_GRACE", 0.05)
    monkeypatch.setattr(agent_pi, "SIGTERM_GRACE", 0.05)

    def use(proc):
        holder["proc"] = proc
        return holder

    return use


def test_build_command_line(tmp_path):
    req = make_request(tmp_path, tools=["read", "bash"], guards=["/g/guard.ts"], prompt="-starts-with-dash")
    cmd = agent_pi.build_command(req)
    assert cmd[:4] == ["pi", "-p", "--mode", "json"]
    for pair in (
        ["--provider", "openrouter"],
        ["--model", "deepseek/deepseek-v4-flash"],
        ["--thinking", "medium"],
        ["--session-id", req.session_id],
        ["--session-dir", req.session_dir],
        ["--system-prompt", req.system_prompt],
        ["--tools", "read,bash"],
        ["-e", "/g/guard.ts"],
    ):
        i = cmd.index(pair[0])
        assert cmd[i : i + 2] == pair
    for flag in ("--no-context-files", "--no-skills", "--no-prompt-templates", "--no-extensions", "--no-approve"):
        assert flag in cmd
    assert cmd[-2:] == ["--", "-starts-with-dash"]


def test_no_tools_means_no_tools_flag(tmp_path):
    assert "--tools" not in agent_pi.build_command(make_request(tmp_path))


def test_empty_tools_list_disables_every_tool(tmp_path):
    cmd = agent_pi.build_command(make_request(tmp_path, tools=[]))
    assert "--no-tools" in cmd
    assert "--tools" not in cmd


def test_no_attachments_ends_in_dash_dash_prompt(tmp_path):
    req = make_request(tmp_path)
    assert agent_pi.build_command(req)[-2:] == ["--", req.prompt]


def test_attachments_land_as_at_path_args_before_the_prompt(tmp_path):
    req = make_request(tmp_path, attachments=["/refs/a.png", "/refs/b.svg"])
    cmd = agent_pi.build_command(req)
    assert cmd[-4:] == ["--", "@/refs/a.png", "@/refs/b.svg", req.prompt]


def test_replayed_ok(tmp_path, fake_pi):
    holder = fake_pi(FakeProc(to_lines(ok_events()), returncode=0))
    events, pids, exits = [], [], []
    req = make_request(tmp_path)
    result = agent_pi.run(req, on_event=events.append, on_spawn=pids.append, on_exit=exits.append)

    assert result.status == "ok"
    assert result.text == "pong"
    assert result.session_id == "s-1"
    assert result.returncode == 0
    assert result.usage.input == 1552
    assert result.usage.total == 1555
    assert result.cost == pytest.approx(1.05e-4)
    assert result.context_tokens == 1552
    assert result.denials == []
    assert pids == [4242] and exits == [4242]
    assert holder["kwargs"]["cwd"] == req.cwd
    assert holder["kwargs"]["stdin"] == agent_pi.DEVNULL


def test_replayed_ok_writes_raw_and_emits_turn(tmp_path, fake_pi):
    lines = to_lines(ok_events())
    fake_pi(FakeProc(lines, returncode=0))
    events = []
    req = make_request(tmp_path)
    agent_pi.run(req, on_event=events.append)

    assert open(req.raw_output_path, "rb").readlines() == lines
    turns = [e for e in events if e["type"] == "turn"]
    assert len(turns) == 1
    assert turns[0]["model"] == MODEL
    assert turns[0]["context_tokens"] == 1552
    assert turns[0]["cost"] == pytest.approx(1.05e-4)


def test_context_tokens_from_last_assistant_message(tmp_path, fake_pi):
    events = ok_events()
    events.insert(4, assistant_message_end(text="first", input=100, cache_read=50, cache_write=7))
    fake_pi(FakeProc(to_lines(events), returncode=0))
    result = agent_pi.run(make_request(tmp_path))
    assert result.context_tokens == 1552  # last message wins, output tokens excluded
    assert result.usage.input == 1652  # sums accumulate across messages
    assert result.text == "pong"


def test_replayed_denied(tmp_path, fake_pi):
    events = ok_events()[:4] + [
        {"type": "tool_execution_start", "toolName": "bash"},
        {"type": "tool_execution_end", "toolName": "bash", "isError": True, "reason": "guard: never-rule *prod*"},
    ]  # guard terminated the run: no agent_settled
    fake_pi(FakeProc(to_lines(events), returncode=1))
    result = agent_pi.run(make_request(tmp_path))
    assert result.status == "denied"
    assert result.denials == [{"tool": "bash", "reason": "guard: never-rule *prod*"}]
    assert result.returncode == 1


def test_terminating_guard_block_is_denied_even_when_settled(tmp_path, fake_pi):
    # 07-guard-probe.md Q4: terminate:true → blocked tool_execution_end, then straight
    # to agent_end/agent_settled with no further assistant message, rc 0
    events = ok_events()[:4] + [
        {"type": "tool_execution_start", "toolName": "bash"},
        {"type": "tool_execution_end", "toolName": "bash", "isError": True, "reason": "guard: blocked"},
        {"type": "agent_end"},
        {"type": "agent_settled"},
    ]
    fake_pi(FakeProc(to_lines(events), returncode=0))
    result = agent_pi.run(make_request(tmp_path))
    assert result.status == "denied"


def test_ordinary_tool_error_answered_by_the_model_is_ok(tmp_path, fake_pi):
    events = ok_events()[:4] + [
        {"type": "tool_execution_start", "toolName": "bash"},
        {"type": "tool_execution_end", "toolName": "bash", "isError": True, "result": "exit 1"},
        assistant_message_end(text="the command failed; done"),
        {"type": "agent_end"},
        {"type": "agent_settled"},
    ]
    fake_pi(FakeProc(to_lines(events), returncode=0))
    result = agent_pi.run(make_request(tmp_path))
    assert result.status == "ok"
    assert result.denials == [{"tool": "bash", "reason": "exit 1"}]


def test_child_env_drops_claude_code_markers(tmp_path, fake_pi, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    holder = fake_pi(FakeProc(to_lines(ok_events()), returncode=0))
    agent_pi.run(make_request(tmp_path))
    env = holder["kwargs"]["env"]
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env


def test_replayed_error_keeps_stderr_tail(tmp_path, fake_pi):
    fake_pi(FakeProc([], returncode=1, stderr=b"Error: unknown model x/y\n"))
    result = agent_pi.run(make_request(tmp_path))
    assert result.status == "error"
    assert "unknown model" in result.stderr_tail


def errored_message_end(msg="Connection error."):
    return {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [],
            "stopReason": "error",
            "errorMessage": msg,
            "usage": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "reasoning": 0,
                "totalTokens": 0,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
        },
    }


def test_a_final_assistant_error_is_status_error_with_the_reason(tmp_path, fake_pi):
    session = {"type": "session", "id": "s-err", "version": 3}
    events = [
        session,
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_end", "message": {"role": "user", "content": []}},
        errored_message_end(),
        {"type": "turn_end"},
        {"type": "agent_end"},
        {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3, "delayMs": 2000, "errorMessage": "Connection error."},
        {"type": "agent_start"},
        {"type": "turn_start"},
        errored_message_end(),
        {"type": "turn_end"},
        {"type": "agent_end"},
        {"type": "auto_retry_end", "success": False, "attempt": 3, "finalError": "Connection error."},
        {"type": "agent_settled"},
    ]
    fake_pi(FakeProc(to_lines(events), returncode=0))
    result = agent_pi.run(make_request(tmp_path))
    assert result.status == "error"
    assert result.error == "Connection error."
    assert result.text == ""


def test_an_error_pi_recovered_from_by_auto_retry_is_ok(tmp_path, fake_pi):
    session = {"type": "session", "id": "s-retry-ok", "version": 3}
    events = [
        session,
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_end", "message": {"role": "user", "content": []}},
        errored_message_end(),
        {"type": "turn_end"},
        {"type": "agent_end"},
        {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3, "delayMs": 2000, "errorMessage": "Connection error."},
        {"type": "agent_start"},
        {"type": "turn_start"},
        assistant_message_end("pong"),
        {"type": "turn_end"},
        {"type": "agent_end"},
        {"type": "agent_settled"},
    ]
    fake_pi(FakeProc(to_lines(events), returncode=0))
    result = agent_pi.run(make_request(tmp_path))
    assert result.status == "ok"
    assert result.error == ""
    assert result.text == "pong"


def test_replayed_max_turns_interrupts_with_sigint(tmp_path, fake_pi):
    events = ok_events()[:2] + [{"type": "turn_start"}] * 3
    proc = FakeProc(to_lines(events), returncode=1, hang=True, dies_on=signal.SIGINT)
    fake_pi(proc)
    result = agent_pi.run(make_request(tmp_path, max_turns=2))
    assert result.status == "max_turns"
    assert proc.signals == [signal.SIGINT]
    assert result.error == "max_turns: 3 turns > cap 2"


def test_parallel_tool_calls_in_one_turn_do_not_count_as_turns(tmp_path, fake_pi):
    events = ok_events()[:2] + tool_turn("t1", 1) + tool_turn("t2", 4) + tool_turn("t3", 1) + [
        {"type": "turn_start"}, assistant_message_end("pong"), {"type": "turn_end"},
        {"type": "agent_end"}, {"type": "agent_settled"},
    ]
    proc = FakeProc(to_lines(events), returncode=0)
    fake_pi(proc)
    result = agent_pi.run(make_request(tmp_path, max_turns=5))
    assert result.status == "ok"
    assert proc.signals == []
    assert result.text == "pong"


def test_max_turns_error_names_cap_and_count_not_the_stderr_tail(tmp_path, fake_pi):
    events = ok_events()[:2] + [{"type": "turn_start"}] * 6
    proc = FakeProc(
        to_lines(events), returncode=1, hang=True, dies_on=signal.SIGINT,
        stderr=b"Warning: No project session found with id 'x'; creating a new session with that id.\n",
    )
    fake_pi(proc)
    result = agent_pi.run(make_request(tmp_path, max_turns=5))
    assert result.status == "max_turns"
    assert result.error == "max_turns: 6 turns > cap 5"
    assert "No project session" not in result.error
    assert "No project session" in result.stderr_tail


def test_five_turns_under_cap_five_complete(tmp_path, fake_pi):
    events = ok_events()[:2] + [
        {"type": "turn_start"}, assistant_message_end("pong"), {"type": "turn_end"},
    ] * 5 + [{"type": "agent_end"}, {"type": "agent_settled"}]
    proc = FakeProc(to_lines(events), returncode=0)
    fake_pi(proc)
    result = agent_pi.run(make_request(tmp_path, max_turns=5))
    assert result.status == "ok"
    assert proc.signals == []


def test_timeout_sends_sigint_before_sigterm(tmp_path, fake_pi):
    proc = FakeProc(to_lines(ok_events()[:2]), returncode=1, hang=True, dies_on=signal.SIGTERM)
    fake_pi(proc)
    result = agent_pi.run(make_request(tmp_path, timeout_s=1))
    assert result.status == "timeout"
    assert proc.signals == [signal.SIGINT, signal.SIGTERM]
    assert result.error == "timeout: 1s elapsed"


def test_tolerates_non_json_lines(tmp_path, fake_pi):
    lines = [b"not json at all\n", *to_lines(ok_events()), b"\n"]
    fake_pi(FakeProc(lines, returncode=0))
    req = make_request(tmp_path)
    result = agent_pi.run(req)
    assert result.status == "ok"
    assert open(req.raw_output_path, "rb").readlines() == lines


def test_context_window_lookup(tmp_path, monkeypatch):
    store = {"openrouter": {"models": [{"id": "deepseek/deepseek-v4-flash", "contextWindow": 1024000}]}}
    (tmp_path / "models-store.json").write_text(json.dumps(store))
    monkeypatch.setattr(config, "PI_AGENT_DIR", tmp_path)
    assert agent_pi.context_window(MODEL) == 1024000
    assert agent_pi.context_window("openrouter/unknown/model") == 0
    # add a models.json with the providers shape
    (tmp_path / "models.json").write_text(json.dumps({
        "providers": {"custom": {"models": [{"id": "m", "contextWindow": 32000}]}}
    }))
    assert agent_pi.context_window("custom/m") == 32000
    # missing dir -> 0
    monkeypatch.setattr(config, "PI_AGENT_DIR", tmp_path / "missing")
    assert agent_pi.context_window(MODEL) == 0


@pytest.mark.real
def test_real_pi_loads_the_packaged_subagent_extension(tmp_path):
    req = make_request(
        tmp_path,
        tools=["read"],
        guards=[config.PI_EXTENSIONS["subagent"]],
        timeout_s=120,
    )
    result = agent_pi.run(req)
    assert result.status == "ok", result.stderr_tail
    assert "Failed to load extension" not in result.stderr_tail


@pytest.mark.real
def test_real_call_returns_generic_output_envelope(tmp_path):
    req = make_request(
        tmp_path,
        system_prompt=(
            "You are a probe agent. Respond with ONLY a fenced ```json block matching "
            '{"status": "success", "summary": "<one sentence>", "artifacts": [], '
            '"notes_for_next_agent": ""} — no prose outside the fence.'
        ),
        prompt="Report that the pi adapter tracer reached you.",
        tools=["read"],
        timeout_s=120,
    )
    events = []
    result = agent_pi.run(req, on_event=events.append)

    assert result.status == "ok", result.stderr_tail
    envelope = extract_envelope(result.text, GenericOutput)
    assert envelope.status == "success"
    assert result.session_id == req.session_id
    assert result.usage.total > 0
    assert result.context_tokens > 0
    assert result.context_window > 0
    assert any(e["type"] == "turn" for e in events)
    assert any(e.get("type") == "agent_settled" for e in events)
    raw = open(req.raw_output_path).read()
    assert '"agent_settled"' in raw


# --- submit tool tests (ticket 74) ---


def test_submit_schema_path_reaches_the_child_env(tmp_path, fake_pi):
    holder = fake_pi(FakeProc(to_lines(ok_events()), returncode=0))
    agent_pi.run(make_request(tmp_path, submit_schema="/s/schema.json"))
    assert holder["kwargs"]["env"]["LIME_SUBMIT_SCHEMA"] == "/s/schema.json"


def test_submit_schema_absent_when_not_set(tmp_path, monkeypatch, fake_pi):
    monkeypatch.delenv("LIME_SUBMIT_SCHEMA", raising=False)
    holder = fake_pi(FakeProc(to_lines(ok_events()), returncode=0))
    agent_pi.run(make_request(tmp_path))
    assert "LIME_SUBMIT_SCHEMA" not in holder["kwargs"]["env"]


@pytest.mark.parametrize("writes", [None, [], ["src/app.py"]], ids=["unset", "readonly", "allowlist"])
@pytest.mark.parametrize("inherited", [True, False], ids=["inherited", "absent"])
def test_child_env_exports_writes(tmp_path, fake_pi, monkeypatch, writes, inherited):
    """The request's writes list round-trips as LIME_WRITES; None removes any
    inherited policy so stale policy never leaks across roles or sessions."""
    if inherited:
        monkeypatch.setenv("LIME_WRITES", json.dumps(["stale/"]))
    else:
        monkeypatch.delenv("LIME_WRITES", raising=False)
    holder = fake_pi(FakeProc(to_lines(ok_events()), returncode=0))
    agent_pi.run(make_request(tmp_path, writes=writes))
    env = holder["kwargs"]["env"]
    if writes is None:
        assert "LIME_WRITES" not in env
    else:
        assert json.loads(env["LIME_WRITES"]) == writes


def test_submit_args_become_the_envelope_with_no_text(tmp_path, fake_pi):
    events = submit_events(("c1", SUBMIT_ARGS, True, ""))
    fake_pi(FakeProc(to_lines(events), returncode=0))
    result = agent_pi.run(make_request(tmp_path))
    assert result.status == "ok"
    assert result.envelope == SUBMIT_ARGS
    assert result.text == ""
    assert result.denials == []


def test_tool_execution_end_becomes_a_tool_result(tmp_path, fake_pi):
    events = ok_events()[:4] + [
        {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "read"},
        {
            "type": "tool_execution_end",
            "toolCallId": "c1",
            "toolName": "read",
            "result": {"content": [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}]},
        },
        {"type": "tool_execution_start", "toolCallId": "c2", "toolName": "bash"},
        {
            "type": "tool_execution_end",
            "toolCallId": "c2",
            "toolName": "bash",
            "isError": True,
            "result": "exit 1",
        },
        assistant_message_end(text="the command failed; done"),
        {"type": "agent_end"},
        {"type": "agent_settled"},
    ]
    fake_pi(FakeProc(to_lines(events), returncode=0))
    seen = []
    result = agent_pi.run(make_request(tmp_path), on_event=seen.append)
    results = [e for e in seen if e.get("type") == "tool_result"]
    assert results == [
        {"type": "tool_result", "tool_call_id": "c1", "tool": "read", "ok": True, "output": "line one\nline two"},
        {"type": "tool_result", "tool_call_id": "c2", "tool": "bash", "ok": False, "output": "exit 1"},
    ]
    assert result.denials == [{"tool": "bash", "reason": "exit 1"}]  # unchanged
    for out in results:
        idx = seen.index(out)
        assert seen[idx - 1]["type"] == "tool_execution_end"
        assert seen[idx - 1]["toolCallId"] == out["tool_call_id"]


def test_submit_tool_result_ok_is_the_envelope_verdict(tmp_path, fake_pi):
    # a rejected submit is not the delivered envelope
    rejected = submit_events(("c1", {"status": "complete"}, False, "status: Expected union value"))
    fake_pi(FakeProc(to_lines(rejected), returncode=0))
    seen = []
    agent_pi.run(make_request(tmp_path), on_event=seen.append)
    assert [e for e in seen if e.get("type") == "tool_result"] == [
        {
            "type": "tool_result",
            "tool_call_id": "c1",
            "tool": "submit",
            "ok": False,
            "output": "status: Expected union value",
        }
    ]

    # the accepted submit is the envelope: ok True, no `{"terminate": True}` output
    accepted = submit_events(("c1", SUBMIT_ARGS, True, ""))
    fake_pi(FakeProc(to_lines(accepted), returncode=0))
    seen = []
    agent_pi.run(make_request(tmp_path), on_event=seen.append)
    assert [e for e in seen if e.get("type") == "tool_result"] == [
        {"type": "tool_result", "tool_call_id": "c1", "tool": "submit", "ok": True, "output": ""}
    ]
    end = next(i for i, e in enumerate(seen) if e.get("type") == "tool_execution_end" and e.get("toolName") == "submit")
    assert [seen[end]["type"], seen[end + 1]["type"], seen[end + 2]["type"]] == [
        "tool_execution_end",
        "tool_result",
        "submit_accepted",
    ]

    # a later accepted submit is not the delivered envelope
    seen = []
    stream = agent_pi._Stream(MODEL, seen.append)
    stream.feed(json.dumps(submit_start("c1", SUBMIT_ARGS)))
    stream.feed(json.dumps(submit_end("c1", ok=True)))
    stream.feed(json.dumps(submit_start("c2", SUBMIT_ARGS)))
    stream.feed(json.dumps(submit_end("c2", ok=True)))
    results = [e for e in seen if e.get("type") == "tool_result"]
    assert [r["ok"] for r in results] == [True, False]


def test_submit_tool_result_ok_is_limes_validation_verdict(tmp_path, fake_pi):
    # an accepted call whose args pass lime's model_validate is a success
    accepted = submit_events(("c1", SUBMIT_ARGS, True, ""))
    fake_pi(FakeProc(to_lines(accepted), returncode=0))
    seen = []
    agent_pi.run(
        make_request(tmp_path, envelope_validator=lambda args: args.get("status") == "success"),
        on_event=seen.append,
    )
    assert [e for e in seen if e.get("type") == "tool_result"] == [
        {"type": "tool_result", "tool_call_id": "c1", "tool": "submit", "ok": True, "output": ""}
    ]

    # the same accepted call, a validator that rejects the args → ok False, never the harness flag
    fake_pi(FakeProc(to_lines(accepted), returncode=0))
    seen = []
    agent_pi.run(
        make_request(tmp_path, envelope_validator=lambda args: False),
        on_event=seen.append,
    )
    assert [e for e in seen if e.get("type") == "tool_result"] == [
        {"type": "tool_result", "tool_call_id": "c1", "tool": "submit", "ok": False, "output": ""}
    ]


def test_no_submit_call_leaves_envelope_none(tmp_path, fake_pi):
    fake_pi(FakeProc(to_lines(ok_events()), returncode=0))
    result = agent_pi.run(make_request(tmp_path))
    assert result.envelope is None
    assert result.submit_error == ""
    assert result.text == "pong"


def test_first_accepted_submit_wins(tmp_path, fake_pi):
    """The FIRST accepted submit is the delivered report: a later accepted submit
    cannot replace it, a later rejection is still recorded for correction, and the
    process is stopped exactly once (one ladder, one acceptance notification)."""
    a = {**SUBMIT_ARGS, "summary": "A"}
    b = {**SUBMIT_ARGS, "summary": "B"}
    events = submit_events(
        ("c1", a, True, ""),
        ("c2", b, True, ""),
        ("c3", {"status": "complete"}, False, "status: Expected union value"),
    )
    proc = FakeProc(to_lines(events), returncode=1, hang=True, dies_on=signal.SIGINT)
    fake_pi(proc)
    seen = []
    result = agent_pi.run(make_request(tmp_path, timeout_s=1800), on_event=seen.append)
    assert result.status == "ok"
    assert result.envelope == a  # first envelope is immutable
    assert proc.signals == [signal.SIGINT]  # exactly one stop ladder
    assert [e["type"] for e in seen if e.get("type") == "submit_accepted"] == ["submit_accepted"]
    assert result.submit_error == "status: Expected union value"  # later rejection kept for correction


def test_an_errored_submit_never_replaces_an_accepted_one(tmp_path, fake_pi):
    a = {**SUBMIT_ARGS, "summary": "A"}
    events = submit_events(("c1", a, True, ""), ("c2", {"status": "complete"}, False, "status: Expected union value"))
    fake_pi(FakeProc(to_lines(events), returncode=0))
    result = agent_pi.run(make_request(tmp_path))
    assert result.envelope == a
    assert result.denials == []
    assert result.submit_error == "status: Expected union value"


def test_a_rejected_submit_then_an_accepted_one_is_ok_not_denied(tmp_path, fake_pi):
    events = submit_events(
        ("c1", {"status": "complete"}, False, "status: Expected union value"),
        ("c2", SUBMIT_ARGS, True, ""),
    )
    fake_pi(FakeProc(to_lines(events), returncode=0))
    result = agent_pi.run(make_request(tmp_path))
    assert result.status == "ok"
    assert result.envelope == SUBMIT_ARGS
    assert result.denials == []


def test_submit_args_are_captured_unclipped(tmp_path, fake_pi):
    long = "x" * 2000
    args = {**SUBMIT_ARGS, "summary": long}
    events = submit_events(("c1", args, True, ""))
    fake_pi(FakeProc(to_lines(events), returncode=0))
    result = agent_pi.run(make_request(tmp_path))
    assert len(result.envelope["summary"]) == 2000


# --- first-acceptance termination (ticket 170) ---


def batched_assistant_message(text, *tool_calls):
    """One assistant message: text plus structured toolCall content parts."""
    return {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}, *tool_calls],
            "usage": {
                "input": 1552, "output": 3, "cacheRead": 0, "cacheWrite": 0,
                "reasoning": 0, "totalTokens": 1555,
                "cost": {"input": 1e-4, "output": 5e-7, "cacheRead": 0, "cacheWrite": 0, "total": 1.05e-4},
            },
        },
    }


def batched_write_submit_events(call_id="c1", args=None, *, settled=False):
    """One assistant message calls write and submit; the write result succeeds without
    terminating. Omit agent_settled so the fake process hangs until signalled."""
    args = SUBMIT_ARGS if args is None else args
    events = [
        {"type": "session", "id": "s-batch", "version": 3},
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_end", "message": {"role": "user", "content": []}},
        batched_assistant_message(
            "writing and reporting",
            {"type": "toolCall", "id": "w1", "name": "write",
             "arguments": {"path": "out.txt", "content": "hi"}},
            {"type": "toolCall", "id": call_id, "name": "submit", "arguments": args},
        ),
        {"type": "tool_execution_start", "toolCallId": "w1", "toolName": "write",
         "args": {"path": "out.txt", "content": "hi"}},
        {"type": "tool_execution_end", "toolCallId": "w1", "toolName": "write",
         "result": {"content": [{"type": "text", "text": "wrote out.txt"}]}},
        submit_start(call_id, args),
        submit_end(call_id, ok=True),
    ]
    if settled:
        events += [{"type": "agent_end"}, {"type": "agent_settled"}]
    return events


class FakeTimer:
    """A manual timer: records its callback, never fires on its own. `fire_on_start` /
    `fire_on_cancel` model a callback dispatched concurrently with run() — before
    acceptance, or already in flight when acceptance cancels the timer."""

    def __init__(self, interval, fn, args=()):
        self.interval = interval
        self.fn = fn
        self.args = args
        self.started = False
        self.cancelled = False
        self.fire_on_start = False
        self.fire_on_cancel = False

    def start(self):
        self.started = True
        if self.fire_on_start:
            self.fire()

    def cancel(self):
        self.cancelled = True
        if self.fire_on_cancel:
            self.fire()

    def fire(self):
        self.fn(*self.args)


class TimerControl:
    """Factory for `agent_pi.threading.Timer`; the test sets `mode` before run()."""

    def __init__(self):
        self.timers: list[FakeTimer] = []
        self.mode = "manual"

    def __call__(self, interval, fn, args=()):
        timer = FakeTimer(interval, fn, args)
        timer.fire_on_start = self.mode == "fire_on_start"
        timer.fire_on_cancel = self.mode == "fire_on_cancel"
        self.timers.append(timer)
        return timer


@pytest.fixture
def fake_timer(monkeypatch):
    control = TimerControl()
    monkeypatch.setattr(agent_pi.threading, "Timer", control)
    return control


def test_batched_write_submit_stops_on_first_acceptance(tmp_path, fake_pi, fake_timer):
    """One assistant message calls write and submit; the write result succeeds without
    terminating and no agent_settled ever arrives. The adapter stops on the accepted
    submit (not the timer), delivers ok / the first envelope, accounts usage and raw
    output, cancels the timer and calls on_exit exactly once."""
    args = {**SUBMIT_ARGS, "summary": "batched"}
    proc = FakeProc(to_lines(batched_write_submit_events(args=args)), returncode=1,
                    hang=True, dies_on=signal.SIGINT)
    fake_pi(proc)
    seen, exits = [], []
    req = make_request(tmp_path, timeout_s=1800)
    result = agent_pi.run(req, on_event=seen.append, on_exit=exits.append)

    assert result.status == "ok"
    assert result.envelope == args
    assert result.error == ""
    assert result.usage.input == 1552
    assert proc.signals == [signal.SIGINT]  # stopped on acceptance, before any timer
    assert [e["type"] for e in seen if e.get("type") == "submit_accepted"] == ["submit_accepted"]
    assert exits == [4242]  # exactly one exit callback
    assert fake_timer.timers[0].cancelled is True  # the controlled timer never fired
    raw = open(req.raw_output_path, "rb").read()
    assert b'"submit"' in raw and b"wrote out.txt" in raw


@pytest.mark.parametrize("mode", ["fire_on_start", "fire_on_cancel"])
def test_accepted_submit_wins_timeout_race(tmp_path, fake_pi, fake_timer, mode):
    """A timeout firing before acceptance, or already in flight when acceptance cancels
    the timer, never replaces the delivered report: a nonzero signal returncode and an
    absent settled event still yield ok with no stale error. A process ignoring SIGINT
    is escalated to SIGTERM and reaped."""
    args = {**SUBMIT_ARGS, "summary": "raced"}
    fake_timer.mode = mode
    proc = FakeProc(to_lines(batched_write_submit_events(args=args)), returncode=1,
                    hang=True, dies_on=signal.SIGTERM)  # ignores SIGINT
    fake_pi(proc)
    seen = []
    result = agent_pi.run(make_request(tmp_path, timeout_s=1800), on_event=seen.append)

    assert result.status == "ok"
    assert result.envelope == args
    assert result.error == ""
    assert result.returncode != 0  # the child exits from a signal
    assert proc.signals == [signal.SIGINT, signal.SIGTERM]  # one ladder, escalated
    assert all(e.get("type") != "agent_settled" for e in seen)
    assert [e["type"] for e in seen if e.get("type") == "submit_accepted"] == ["submit_accepted"]


def test_rejection_and_start_alone_are_not_acceptance(tmp_path, fake_pi):
    """A submit start, an unmatched end, missing args and a schema rejection are never
    acceptance: no signal, no notification. An empty dict IS a dict (is not None) and
    does latch acceptance — exactly one stop on that end."""
    events = [
        {"type": "session", "id": "s-r", "version": 3},
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_end", "message": {"role": "user", "content": []}},
        assistant_message_end(text=""),
        submit_start("c0", SUBMIT_ARGS),  # start alone — no end
        submit_end("c-unknown", ok=True),  # unmatched end
        {"type": "tool_execution_start", "toolCallId": "c-miss", "toolName": "submit"},  # no args
        submit_end("c-miss", ok=True),
        submit_start("c1", {"status": "complete"}),
        submit_end("c1", ok=False, reason="status: Expected union value"),
        submit_start("c2", {}),
        submit_end("c2", ok=True),
    ]
    proc = FakeProc(to_lines(events), returncode=1, hang=True, dies_on=signal.SIGINT)
    fake_pi(proc)
    seen = []
    result = agent_pi.run(make_request(tmp_path, timeout_s=1800), on_event=seen.append)

    assert result.envelope == {}
    assert proc.signals == [signal.SIGINT]  # exactly one stop, on the accepted end
    assert [e["type"] for e in seen if e.get("type") == "submit_accepted"] == ["submit_accepted"]
    assert result.submit_error == "status: Expected union value"


def test_trailing_invalid_utf8_after_acceptance_still_delivers(tmp_path, fake_pi):
    """A decode failure in the trailing stream after acceptance must not discard the
    delivered report: cleanup stays bounded and the envelope is returned."""
    args = {**SUBMIT_ARGS, "summary": "trailing"}
    lines = to_lines(batched_write_submit_events(args=args)) + [b"\xff\xff\n"]
    proc = FakeProc(lines, returncode=1, hang=True, dies_on=signal.SIGINT)
    fake_pi(proc)
    result = agent_pi.run(make_request(tmp_path, timeout_s=1800))
    assert result.status == "ok"
    assert result.envelope == args
    assert result.error == ""


def test_build_command_line_with_submit_schema(tmp_path):
    req = make_request(tmp_path, submit_schema="/s/schema.json")
    cmd = agent_pi.build_command(req)
    assert "--tools" not in cmd or "submit" not in " ".join(cmd)
    assert "LIME_SUBMIT_SCHEMA" not in " ".join(cmd)


@pytest.mark.real
def test_real_pi_submit_delivers_a_nested_envelope(tmp_path):
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(agents.submit_schema(ReviewOutput), indent=2))
    req = make_request(
        tmp_path,
        tools=["read", "submit"],
        guards=[config.SUBMIT_EXTENSION],
        submit_schema=str(schema_path),
        system_prompt="You are a probe. Finish by calling the submit tool with your report; do not write JSON as text.",
        prompt="Review this claim: 'the sky is blue' — met, evidence 'daylight'. status success, approved true, one finding. Call submit.",
        timeout_s=120,
    )
    result = agent_pi.run(req)
    assert result.status == "ok", result.stderr_tail
    assert "Failed to load extension" not in result.stderr_tail
    envelope = ReviewOutput.model_validate(result.envelope)
    assert len(envelope.findings) > 0


# --- tool_choice tests ---


def _run_submit_ts_offline(tmp_path, tool_choice_env=None):
    """Launch bare node with submit.ts, registering a fake pi API, and invoke
    the before_provider_request hook. Returns the synthetic payload after
    the hook runs (or the original if no hook registered)."""
    import subprocess, json, os
    from pathlib import Path
    submit_ts_path = Path(__file__).resolve().parent.parent / "lime" / "harness_engineering" / "submit.ts"
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps({"type": "object", "properties": {"status": {"type": "string"}, "summary": {"type": "string"}}, "required": ["status", "summary"]}))

    # Write the script to a temp file to avoid shell escaping issues
    script_path = tmp_path / "test_submit_ts.mjs"
    script_path.write_text(f'''
import {{ pathToFileURL }} from "node:url";
let hooks = [];
let tools = [];
const fakePi = {{
    registerTool: (desc) => {{ tools.push(desc.name); }},
    on: (event, handler) => {{ hooks.push({{ event, handler }}); }},
}};
const mod = await import(pathToFileURL("{submit_ts_path}").href);
mod.default(fakePi);
const payload = {{
    model: "test-model",
    messages: [{{ role: "user", content: "hi" }}],
    tools: tools.map(t => ({{ function: {{ name: t }}, type: "function" }})),
    extra_field: "preserve-me",
}};
const event = {{ payload: {{ ...payload }} }};
const ctx = {{ model: "test-model" }};
let resultPayload = {{ ...payload }};
for (const h of hooks) {{
    const ret = await h.handler(event, ctx);
    if (ret !== undefined) {{
        resultPayload = ret;
    }}
}}
process.stdout.write(JSON.stringify(resultPayload));
''')

    env = os.environ.copy()
    env.pop("LIME_TOOL_CHOICE", None)
    if tool_choice_env is not None:
        env["LIME_TOOL_CHOICE"] = tool_choice_env
    env["LIME_SUBMIT_SCHEMA"] = str(schema_path)
    result = subprocess.run(
        ["node", str(script_path)],
        capture_output=True, text=True, check=True, env=env, timeout=30,
    )
    return json.loads(result.stdout)


@needs_node
def test_required_tool_choice_reaches_provider_payload(tmp_path):
    """Required env setting adds tool_choice: required to the provider payload,
    preserving model, messages, tools, and unrelated fields."""
    payload = _run_submit_ts_offline(tmp_path, tool_choice_env="required")
    assert payload["tool_choice"] == "required"
    assert payload["model"] == "test-model"
    assert len(payload["messages"]) == 1
    assert len(payload["tools"]) == 1
    assert payload["extra_field"] == "preserve-me"


@needs_node
def test_auto_tool_choice_omits_provider_payload_field(tmp_path):
    """Auto (unset) env does NOT inject tool_choice, not even null or 'auto'."""
    payload = _run_submit_ts_offline(tmp_path, tool_choice_env=None)
    assert "tool_choice" not in payload, f"got {payload}"
    assert payload["model"] == "test-model"
    assert payload["extra_field"] == "preserve-me"
    # Explicit auto also omits
    payload2 = _run_submit_ts_offline(tmp_path, tool_choice_env="auto")
    assert "tool_choice" not in payload2, f"got {payload2}"


def test_tool_choice_env_is_per_request(tmp_path, fake_pi):
    """Both choices produce correct LIME_TOOL_CHOICE in captured Popen env,
    overriding any inherited value."""
    # Required with submit_schema set
    holder = fake_pi(FakeProc(to_lines(ok_events()), returncode=0))
    agent_pi.run(make_request(tmp_path, tool_choice="required", submit_schema="/tmp/schema.json"))
    env = holder["kwargs"]["env"]
    assert env["LIME_TOOL_CHOICE"] == "required"
    assert env["LIME_SUBMIT_SCHEMA"] == "/tmp/schema.json"

    # Auto with submit_schema set
    holder2 = fake_pi(FakeProc(to_lines(ok_events()), returncode=0))
    agent_pi.run(make_request(tmp_path, tool_choice="auto", submit_schema="/tmp/schema.json"))
    env2 = holder2["kwargs"]["env"]
    assert env2["LIME_TOOL_CHOICE"] == "auto"

    # Verify LIME_TOOL_CHOICE env is overridden
    import os
    old_val = os.environ.get("LIME_TOOL_CHOICE")
    os.environ["LIME_TOOL_CHOICE"] = "required"
    holder3 = fake_pi(FakeProc(to_lines(ok_events()), returncode=0))
    agent_pi.run(make_request(tmp_path, tool_choice="auto"))
    env3 = holder3["kwargs"]["env"]
    assert env3["LIME_TOOL_CHOICE"] == "auto", f"expected auto, got {env3['LIME_TOOL_CHOICE']}"
    if old_val is None:
        del os.environ["LIME_TOOL_CHOICE"]
    else:
        os.environ["LIME_TOOL_CHOICE"] = old_val


# --- has_tool_calls tests ---


_GUARD_PROBE_JS = '''
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

// Stub child_process.spawnSync BEFORE importing guard.ts so its named import of
// spawnSync binds to the fake (a CJS module's named exports snapshot the patched
// property at first import).
const require = createRequire(import.meta.url);
const cp = require("node:child_process");
const replies = {
  crash: { status: 1, stdout: "" },
  empty: { status: 0, stdout: "" },
  invalid: { status: 0, stdout: "not json" },
  deny: { status: 0, stdout: JSON.stringify({ hookSpecificOutput: { permissionDecision: "deny", permissionDecisionReason: "outside boundary" } }) },
};
globalThis.__reply = replies.crash;
cp.spawnSync = () => globalThis.__reply;

const mod = await import(pathToFileURL(process.argv[2]).href);
const handlers = {};
const fakePi = { on: (name, fn) => { handlers[name] = fn; }, sendMessage: () => {}, registerTool: () => {} };
mod.default(fakePi);
const handler = handlers["tool_call"];
const out = {};
for (const [name, reply] of Object.entries(replies)) {
  globalThis.__reply = reply;
  let r;
  try { r = handler({ toolName: "write", input: { path: "src/app.py" } }); } catch (e) { r = { error: String(e) }; }
  out[name] = { block: Boolean(r && r.block), reason: r && r.reason };
}
process.stdout.write(JSON.stringify(out));
'''


@needs_node
def test_guard_extension_crash_fails_open(tmp_path):
    """A crashed or malformed `lime guard` delivery must never block a tool call:
    guard.ts returns no block for nonzero status, empty output, and invalid JSON,
    while a real deny response still blocks."""
    import subprocess

    guard_ts = agents.GUARD_DIR / "guard.ts"
    script = tmp_path / "guard_probe.mjs"
    script.write_text(_GUARD_PROBE_JS)
    result = subprocess.run(
        ["node", str(script), str(guard_ts)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    for name in ("crash", "empty", "invalid"):
        assert out[name]["block"] is False, (name, out)
    # Control: a deny response still blocks.
    assert out["deny"]["block"] is True
    assert out["deny"]["reason"] == "outside boundary"


def _leak_events(text):
    """Events for a single assistant message with the given text, no submit call."""
    return [
        {"type": "session", "id": "s-leak", "version": 3},
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_end", "message": {"role": "user", "content": []}},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "usage": {"input": 1552, "output": 3, "cacheRead": 0, "cacheWrite": 0,
                          "reasoning": 0, "totalTokens": 1555,
                          "cost": {"input": 1e-4, "output": 5e-7, "cacheRead": 0, "cacheWrite": 0, "total": 1.05e-4}},
            },
        },
        {"type": "turn_end"},
        {"type": "agent_end"},
        {"type": "agent_settled"},
    ]


def test_replayed_text_tool_call_leak_has_no_structured_call(tmp_path, fake_pi):
    """Both marker forms yield retained text, no envelope/rejection, and False has_tool_calls;
    a real toolCall part yields True, and a later text-only assistant reply resets it to False."""
    # <function= marker (dropped outer tag)
    func_text = 'Here is my report: <function=submit>{"status":"success"}</function>'
    fake_pi(FakeProc(to_lines(_leak_events(func_text)), returncode=0))
    result = agent_pi.run(make_request(tmp_path))
    assert result.envelope is None
    assert result.submit_error == ""
    assert result.has_tool_calls is False
    assert "<function=submit>" in result.text

    # <tool_call> marker
    tc_text = 'Here is my call: <tool_call><function=submit>{"status":"success"}</tool_call>'
    fake_pi(FakeProc(to_lines(_leak_events(tc_text)), returncode=0))
    result2 = agent_pi.run(make_request(tmp_path))
    assert result2.envelope is None
    assert result2.submit_error == ""
    assert result2.has_tool_calls is False
    assert "<tool_call>" in result2.text

    # Real toolCall part
    tool_call_events = [
        {"type": "session", "id": "s-ct", "version": 3},
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_end", "message": {"role": "user", "content": []}},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I will call a tool"},
                    {"type": "toolCall", "id": "call_1", "name": "bash", "arguments": {"command": "ls"}},
                ],
                "usage": {"input": 10, "output": 5, "cacheRead": 0, "cacheWrite": 0,
                          "reasoning": 0, "totalTokens": 15,
                          "cost": {"input": 1e-4, "output": 5e-7, "cacheRead": 0, "cacheWrite": 0, "total": 1.05e-4}},
            },
        },
        {"type": "turn_end"},
        {"type": "agent_end"},
        {"type": "agent_settled"},
    ]
    fake_pi(FakeProc(to_lines(tool_call_events), returncode=0))
    result3 = agent_pi.run(make_request(tmp_path))
    assert result3.has_tool_calls is True

    # A later text-only assistant reply resets it to False
    fake_pi(FakeProc(to_lines(_leak_events("final answer, no tools")), returncode=0))
    result4 = agent_pi.run(make_request(tmp_path))
    assert result4.has_tool_calls is False
    assert result4.text == "final answer, no tools"


# --- late denial vs. accepted report (ticket 171) ---


def late_denial_events(args, *, before=False, later_submit=None, settled=True):
    """An accepted report, then two errored non-submit tool completions with no later
    assistant message — the terminal-looking event order the adapter must not reclassify."""
    events = ok_events()[:4] + [assistant_message_end(text="")]
    if before:
        events += [
            {"type": "tool_execution_start", "toolName": "write"},
            {"type": "tool_execution_end", "toolName": "write", "isError": True, "reason": "guard: blocked write"},
        ]
    events += [submit_start("c1", args), submit_end("c1", ok=True)]
    if later_submit is not None:
        events += [submit_start("c2", later_submit), submit_end("c2", ok=True)]
    events += [
        {"type": "tool_execution_start", "toolName": "read"},
        {"type": "tool_execution_end", "toolName": "read", "isError": True, "reason": "guard: blocked read"},
        {"type": "tool_execution_start", "toolName": "bash"},
        {"type": "tool_execution_end", "toolName": "bash", "isError": True, "reason": "guard: blocked bash"},
    ]
    if settled:
        events += [{"type": "agent_end"}, {"type": "agent_settled"}]
    return events


def _pi_warnings(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "lime.agent_pi" and r.levelno == logging.WARNING
    ]


@pytest.mark.parametrize("status", ["success", "partial", "fail"])
@pytest.mark.parametrize("before", [False, True], ids=["after", "before"])
def test_late_denial_keeps_accepted_submit(tmp_path, fake_pi, caplog, status, before):
    caplog.set_level(logging.WARNING, logger="lime.agent_pi")
    args = {**SUBMIT_ARGS, "status": status, "summary": status}
    proc = FakeProc(to_lines(late_denial_events(args, before=before)), returncode=1,
                    hang=True, dies_on=signal.SIGINT)
    fake_pi(proc)
    seen = []
    result = agent_pi.run(make_request(tmp_path, timeout_s=1800), on_event=seen.append)

    assert result.status == "ok"
    assert result.envelope == args  # exact first envelope/status
    assert result.error == ""
    expected = ([{"tool": "write", "reason": "guard: blocked write"}] if before else []) + [
        {"tool": "read", "reason": "guard: blocked read"},
        {"tool": "bash", "reason": "guard: blocked bash"},
    ]
    assert result.denials == expected  # audit entries unchanged
    tools = (["write"] if before else []) + ["read", "bash"]
    warnings = _pi_warnings(caplog)
    assert len(warnings) == len(tools)  # one per denied tool
    for tool in tools:
        assert any(tool in m for m in warnings)
    assert [e["type"] for e in seen if e.get("type") == "submit_accepted"] == ["submit_accepted"]
    assert proc.signals == [signal.SIGINT]  # one acceptance, one stop ladder


def test_late_denial_keeps_first_accepted_submit(tmp_path, fake_pi, caplog):
    caplog.set_level(logging.WARNING, logger="lime.agent_pi")
    first = {**SUBMIT_ARGS, "summary": "first"}
    second = {**SUBMIT_ARGS, "summary": "second"}
    proc = FakeProc(to_lines(late_denial_events(first, later_submit=second)), returncode=1,
                    hang=True, dies_on=signal.SIGINT)
    fake_pi(proc)
    seen = []
    result = agent_pi.run(make_request(tmp_path, timeout_s=1800), on_event=seen.append)

    assert result.status == "ok"
    assert result.envelope == first  # a later accepted submit cannot replace the first report
    assert proc.signals == [signal.SIGINT]  # still one ladder
    assert [e["type"] for e in seen if e.get("type") == "submit_accepted"] == ["submit_accepted"]
    assert len(_pi_warnings(caplog)) == 2


@pytest.mark.parametrize("rejected", [False, True], ids=["no_submit", "rejected_submit"])
@pytest.mark.parametrize("settled", [False, True], ids=["open", "settled"])
def test_late_denial_without_accepted_submit_stays_denied(tmp_path, fake_pi, caplog, rejected, settled):
    caplog.set_level(logging.WARNING, logger="lime.agent_pi")
    events = ok_events()[:4] + [assistant_message_end(text="")]
    if rejected:
        events += [
            submit_start("c1", {"status": "complete"}),
            submit_end("c1", ok=False, reason="status: Expected union value"),
        ]
    events += [
        {"type": "tool_execution_start", "toolName": "bash"},
        {"type": "tool_execution_end", "toolName": "bash", "isError": True, "reason": "guard: blocked"},
    ]
    if settled:
        events += [{"type": "agent_end"}, {"type": "agent_settled"}]
    fake_pi(FakeProc(to_lines(events), returncode=1))
    result = agent_pi.run(make_request(tmp_path))

    assert result.status == "denied"
    assert result.envelope is None
    assert result.denials == [{"tool": "bash", "reason": "guard: blocked"}]
    if rejected:
        assert result.submit_error == "status: Expected union value"  # kept, not counted a denial
    else:
        assert result.submit_error == ""
    assert _pi_warnings(caplog) == []  # no report delivered, so no ignored-denial warning


def test_late_denial_does_not_leak_between_runs(tmp_path, fake_pi, caplog):
    caplog.set_level(logging.WARNING, logger="lime.agent_pi")
    args = {**SUBMIT_ARGS, "summary": "first"}
    fake_pi(FakeProc(to_lines(late_denial_events(args)), returncode=1, hang=True, dies_on=signal.SIGINT))
    assert agent_pi.run(make_request(tmp_path, timeout_s=1800)).status == "ok"
    assert len(_pi_warnings(caplog)) == 2

    caplog.clear()
    # a later run without a report cannot inherit the exemption or emit the old warnings
    denial_events = ok_events()[:4] + [
        {"type": "tool_execution_start", "toolName": "bash"},
        {"type": "tool_execution_end", "toolName": "bash", "isError": True, "reason": "guard: blocked"},
    ]
    fake_pi(FakeProc(to_lines(denial_events), returncode=1))
    second = agent_pi.run(make_request(tmp_path))
    assert second.status == "denied"
    assert _pi_warnings(caplog) == []


def test_should_stop_ends_the_run_as_timeout(tmp_path, fake_pi, fake_timer):
    """The orchestrator's per-Session stop (ticket 173) ends the child like the time
    ceiling: status timeout, one bounded SIGINT ladder, the timer cancelled and never
    fired, with no accepted envelope in play."""
    flag = {"on": False}
    seen = {"n": 0}
    events = [
        {"type": "session", "id": "s-stop", "version": 3},
        *[
            {"type": "tool_execution_start", "toolName": "write", "toolCallId": f"w{i}",
             "args": {"path": "a.txt", "content": "x"}}
            for i in range(3)
        ],
    ]
    proc = FakeProc(to_lines(events), returncode=1, hang=True)
    fake_pi(proc)

    def on_event(event):
        if event.get("type") == "tool_execution_start":
            seen["n"] += 1
            if seen["n"] >= 3:
                flag["on"] = True

    req = make_request(tmp_path, timeout_s=1800).model_copy(
        update={"should_stop": lambda: flag["on"]}
    )
    result = agent_pi.run(req, on_event=on_event)

    assert result.status == "timeout"
    assert proc.signals == [signal.SIGINT]  # one ladder, no escalation
    assert fake_timer.timers and fake_timer.timers[0].started is True
    assert fake_timer.timers[0].cancelled is True  # never fired


def test_accepted_submit_outranks_should_stop(tmp_path, fake_pi, fake_timer):
    """A delivered pi envelope still wins when should_stop is always True: status
    precedence puts the accepted submit first, so the run ends ok with the envelope."""
    args = {**SUBMIT_ARGS, "summary": "delivered"}
    proc = FakeProc(to_lines(batched_write_submit_events(args=args)), returncode=1,
                    hang=True, dies_on=signal.SIGINT)
    fake_pi(proc)
    req = make_request(tmp_path, timeout_s=1800).model_copy(
        update={"should_stop": lambda: True}
    )
    result = agent_pi.run(req)

    assert result.status == "ok"
    assert result.envelope == args
    assert result.error == ""
    assert proc.signals == [signal.SIGINT]


def test_child_env_sets_runtime_for_guard(tmp_path, fake_pi, monkeypatch):
    """The guard selects its partial-delivery verb by runtime: the adapter forces its own
    marker before spawning, overriding any inherited one."""
    monkeypatch.setenv("LIME_AGENT_RUNTIME", "claude_code")  # opposite of this adapter's runtime
    holder = fake_pi(FakeProc(to_lines(ok_events()), returncode=0))
    agent_pi.run(make_request(tmp_path))
    assert holder["kwargs"]["env"]["LIME_AGENT_RUNTIME"] == "pi"


# --- builder bash deadline (ticket 198) ---


def test_child_env_carries_the_bash_deadline_only_when_set(tmp_path, fake_pi, monkeypatch):
    """The request's bash_deadline_s becomes LIME_BASH_DEADLINE_S; None removes any
    inherited operator value so a non-builder Session never keeps a stale cap."""
    monkeypatch.setenv("LIME_BASH_DEADLINE_S", "7")

    holder = fake_pi(FakeProc(to_lines(ok_events()), returncode=0))
    agent_pi.run(make_request(tmp_path))
    assert "LIME_BASH_DEADLINE_S" not in holder["kwargs"]["env"]

    holder = fake_pi(FakeProc(to_lines(ok_events()), returncode=0))
    agent_pi.run(make_request(tmp_path, bash_deadline_s=900))
    assert holder["kwargs"]["env"]["LIME_BASH_DEADLINE_S"] == "900"


def test_a_bash_timeout_error_does_not_end_the_session(tmp_path, fake_pi):
    """A capped `bash` call comes back as an isError tool result (pi's timeout text);
    the adapter records it as a denial but the Session keeps going to a delivered submit."""
    events = ok_events()[:4] + [
        {"type": "tool_execution_start", "toolName": "bash"},
        {
            "type": "tool_execution_end",
            "toolName": "bash",
            "isError": True,
            "result": {
                "content": [{"type": "text", "text": "partial\n\nCommand timed out after 900 seconds"}],
                "details": {},
            },
        },
        assistant_message_end(text=""),
        submit_start("c1", SUBMIT_ARGS),
        submit_end("c1"),
        {"type": "agent_end"},
        {"type": "agent_settled"},
    ]
    fake_pi(FakeProc(to_lines(events), returncode=0))
    result = agent_pi.run(make_request(tmp_path))

    assert result.status == "ok"
    assert result.envelope == SUBMIT_ARGS
    assert len(result.denials) == 1
    assert result.denials[0]["tool"] == "bash"
    assert "Command timed out after 900 seconds" in result.denials[0]["reason"]


_GUARD_DEADLINE_PROBE_JS = '''
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

// Stub child_process.spawnSync BEFORE importing guard.ts so its named import of
// spawnSync binds to the fake (a CJS module's named exports snapshot the patched
// property at first import).
const require = createRequire(import.meta.url);
const cp = require("node:child_process");
const replies = {
  crash: { status: 1, stdout: "" },
  empty: { status: 0, stdout: "" },
  invalid: { status: 0, stdout: "not json" },
  allow: { status: 0, stdout: JSON.stringify({ hookSpecificOutput: { permissionDecision: "allow", additionalContext: "warn" } }) },
  deny: { status: 0, stdout: JSON.stringify({ hookSpecificOutput: { permissionDecision: "deny", permissionDecisionReason: "outside boundary" } }) },
};
globalThis.__reply = replies.allow;
cp.spawnSync = (_c, _a, opts) => { globalThis.__seen = opts.input; return globalThis.__reply; };

const cases = {
  absent: { toolName: "bash", input: { command: "x" } },
  above: { toolName: "bash", input: { command: "x", timeout: 901 } },
  equal: { toolName: "bash", input: { command: "x", timeout: 900 } },
  below: { toolName: "bash", input: { command: "x", timeout: 5 } },
  text: { toolName: "bash", input: { command: "x", timeout: "60" } },
  read: { toolName: "read", input: { path: "a" } },
};

const mod = await import(pathToFileURL(process.argv[2]).href);
const handlers = {};
const fakePi = { on: (name, fn) => { handlers[name] = fn; }, sendMessage: () => {}, registerTool: () => {} };
mod.default(fakePi);
const handler = handlers["tool_call"];

const out = {};
for (const [replyName, reply] of Object.entries(replies)) {
  globalThis.__reply = reply;
  out[replyName] = {};
  for (const [caseName, c] of Object.entries(cases)) {
    const input = structuredClone(c.input);
    globalThis.__seen = "";
    let r;
    try { r = handler({ toolName: c.toolName, input }); } catch (e) { r = { error: String(e) }; }
    out[replyName][caseName] = {
      timeout: input.timeout,
      hasTimeout: "timeout" in input,
      block: Boolean(r && r.block),
      seen: JSON.parse(globalThis.__seen).tool_input,
    };
  }
}
process.stdout.write(JSON.stringify(out));
'''

_DEADLINE_CASE_INPUTS = {
    "absent": {"command": "x"},
    "above": {"command": "x", "timeout": 901},
    "equal": {"command": "x", "timeout": 900},
    "below": {"command": "x", "timeout": 5},
    "text": {"command": "x", "timeout": "60"},
    "read": {"path": "a"},
}

_REPLIES = ("crash", "empty", "invalid", "allow", "deny")


def _run_deadline_probe(tmp_path, deadline):
    guard_ts = agents.GUARD_DIR / "guard.ts"
    script = tmp_path / "guard_deadline_probe.mjs"
    script.write_text(_GUARD_DEADLINE_PROBE_JS)
    env = {**os.environ}
    if deadline is None:
        env.pop("LIME_BASH_DEADLINE_S", None)
    else:
        env["LIME_BASH_DEADLINE_S"] = deadline
    result = subprocess.run(
        ["node", str(script), str(guard_ts)], capture_output=True, text=True, timeout=60, env=env
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
def test_guard_caps_a_bash_timeout_at_the_deadline(tmp_path):
    """The deadline cap runs before the `lime guard` call, so a crashed, empty or invalid
    Guard still caps a `bash` call; a smaller model timeout is kept, and the payload the
    Guard sees stays uncapped."""
    out = _run_deadline_probe(tmp_path, "900")
    capped = {"absent": 900, "above": 900, "equal": 900, "below": 5, "text": 900}
    for reply in _REPLIES:
        for case, timeout in capped.items():
            assert out[reply][case]["timeout"] == timeout, (reply, case, out[reply][case])
            assert out[reply][case]["hasTimeout"] is True
        assert out[reply]["read"]["hasTimeout"] is False
        for case, original in _DEADLINE_CASE_INPUTS.items():
            assert out[reply][case]["seen"] == original, (reply, case, out[reply][case]["seen"])
    # Control: a deny response still blocks.
    assert out["deny"]["below"]["block"] is True


@needs_node
@pytest.mark.parametrize("deadline", [None, "", "abc", "0", "-5", "1.5"])
def test_guard_leaves_bash_alone_without_a_valid_deadline(tmp_path, deadline):
    """An absent, empty, non-integer or <=0 LIME_BASH_DEADLINE_S means no cap: every
    call's timeout (and its presence) is exactly what the model sent."""
    out = _run_deadline_probe(tmp_path, deadline)
    for reply in _REPLIES:
        for case, original in _DEADLINE_CASE_INPUTS.items():
            record = out[reply][case]
            assert record["hasTimeout"] == ("timeout" in original), (reply, case, record)
            assert record.get("timeout") == original.get("timeout"), (reply, case, record)


_BASH_DEADLINE_REAL_JS = '''
import { pathToFileURL } from "node:url";

const mod = await import(pathToFileURL(process.argv[2]).href);
const handlers = {};
const fakePi = { on: (name, fn) => { handlers[name] = fn; }, sendMessage: () => {}, registerTool: () => {} };
mod.default(fakePi);
const handler = handlers["tool_call"];

const { createBashTool } = await import(pathToFileURL(process.argv[3]).href);
const bash = createBashTool(process.cwd());

async function probe(input) {
  const event = { toolName: "bash", toolCallId: "c", input };
  handler(event);
  const start = Date.now();
  let ok = true;
  let text = "";
  try {
    const res = await bash.execute("c", event.input);
    text = (res.content || []).map((c) => c.text).join("");
  } catch (e) {
    ok = false;
    text = String(e && e.message ? e.message : e);
  }
  return { ok, text, ms: Date.now() - start, timeout: event.input.timeout };
}

const out = {
  hung: await probe({ command: "sleep 30" }),
  fast: await probe({ command: "echo done" }),
};
process.stdout.write(JSON.stringify(out));
'''


def _pi_package():
    pi = shutil.which("pi")
    assert pi
    for parent in Path(pi).resolve().parents:
        pkg = parent / "package.json"
        if not pkg.exists():
            continue
        try:
            if json.loads(pkg.read_text()).get("name") == "@earendil-works/pi-coding-agent":
                return parent
        except json.JSONDecodeError:
            continue
    raise AssertionError("pi package @earendil-works/pi-coding-agent not found")


@pytest.mark.skipif(
    shutil.which("pi") is None or shutil.which("node") is None,
    reason="pi and node are not both on PATH (drives the real pi package's bash tool)",
)
def test_a_hung_bash_call_comes_back_as_the_timeout_text(tmp_path):
    """Against pi's real bash tool: the guard's deadline caps a hung call, which pi kills
    and reports as the timeout text, while a fast call runs untouched. No API is billed."""
    guard_ts = agents.GUARD_DIR / "guard.ts"
    pi_index = _pi_package() / "dist" / "index.js"
    script = tmp_path / "bash_deadline_real.mjs"
    script.write_text(_BASH_DEADLINE_REAL_JS)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    lime_stub = bindir / "lime"
    lime_stub.write_text("#!/bin/sh\nexit 0\n")
    lime_stub.chmod(0o755)
    env = {**os.environ, "LIME_BASH_DEADLINE_S": "1", "PATH": f"{bindir}:{os.environ['PATH']}"}
    result = subprocess.run(
        ["node", str(script), str(guard_ts), str(pi_index)],
        cwd=tmp_path, capture_output=True, text=True, timeout=60, env=env,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["hung"]["ok"] is False
    assert out["hung"]["text"].endswith("Command timed out after 1 seconds")
    assert out["hung"]["timeout"] == 1
    assert out["hung"]["ms"] < 10000
    assert out["fast"]["ok"] is True
    assert out["fast"]["text"] == "done\n"
