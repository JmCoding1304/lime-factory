"""Replayed-stream tests: every AgentResult status without spawning claude, plus one real call."""

import ast
import json
import logging
import signal
import threading
import uuid
from io import StringIO
from pathlib import Path
from subprocess import TimeoutExpired

import pytest

from lime import agent_cc, agent_pi, tracer
from lime.agents import extract_envelope
from lime.data_types import AgentRequest, GenericOutput

MODEL = "haiku"


def make_request(tmp_path, **overrides) -> AgentRequest:
    fields: dict = dict(
        runtime="claude_code",
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


def init_event(session_id="cc-1"):
    return {
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
        "apiKeySource": "none",
        "tools": ["Read", "Bash"],
        "model": "claude-haiku-4-5-20251001",
    }


def assistant_event(text="pong", message_id="msg_1", input=4, cache_read=100, cache_write=4500):
    return {
        "type": "assistant",
        "session_id": "cc-1",
        "message": {
            "id": message_id,
            "role": "assistant",
            "content": [{"type": "text", "text": text}] if text else [],
            "usage": {
                "input_tokens": input,
                "output_tokens": 3,
                "cache_creation_input_tokens": cache_write,
                "cache_read_input_tokens": cache_read,
                "output_tokens_details": {"thinking_tokens": 2},
            },
        },
    }


def result_event(subtype="success", is_error=False, text="pong", denials=(), cost=0.0123,
                 input=4, cache_read=100, cache_write=4500):
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": text,
        "session_id": "cc-1",
        "num_turns": 1,
        "total_cost_usd": cost,
        "usage": {
            "input_tokens": input,
            "output_tokens": 3,
            "cache_creation_input_tokens": cache_write,
            "cache_read_input_tokens": cache_read,
        },
        "modelUsage": {"claude-haiku-4-5-20251001": {"contextWindow": 200000}},
        "permission_denials": list(denials),
    }


def ok_events(session_id="cc-1"):
    return [init_event(session_id), assistant_event(), result_event()]


def to_lines(events):
    return [json.dumps(e) + "\n" for e in events]


_LADDER = [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]


class FakeProc:
    """Stands in for Popen: replays a stream, optionally hangs until signalled."""

    def __init__(self, lines, returncode=0, hang=False, dies_on=signal.SIGINT, stderr=""):
        self.pid = 4242
        self.signals: list[int] = []
        self.returncode = None
        self._rc = returncode
        self._dies_on = dies_on
        self._dead = threading.Event()
        if not hang:
            self._dead.set()
        self.stderr = StringIO(stderr)
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
            raise TimeoutExpired(cmd="claude", timeout=timeout or 0)
        self.returncode = self._rc
        return self._rc


@pytest.fixture
def short_grace(monkeypatch):
    """The shared ladder resolves its grace constants in `agent_pi`, its defining module."""
    monkeypatch.setattr(agent_pi, "SIGINT_GRACE", 0.05)
    monkeypatch.setattr(agent_pi, "SIGTERM_GRACE", 0.05)


@pytest.fixture
def fake_cc(monkeypatch, short_grace):
    """Replaces Popen; returns a setter for the FakeProc the next run() gets."""
    holder = {}

    def popen(cmd, **kwargs):
        holder["cmd"], holder["kwargs"] = cmd, kwargs
        return holder["proc"]

    monkeypatch.setattr(agent_cc, "Popen", popen)

    def use(proc):
        holder["proc"] = proc
        return holder

    return use


def test_build_command_first_send(tmp_path):
    req = make_request(tmp_path, tools=["Read", "Bash"], guards=["/g/guard.sh"], max_turns=7)
    cmd = agent_cc.build_command(req)
    assert cmd[:3] == ["claude", "-p", req.prompt]
    for pair in (
        ["--output-format", "stream-json"],
        ["--model", "haiku"],
        ["--session-id", req.session_id],
        ["--system-prompt", req.system_prompt],
        ["--effort", "medium"],
        ["--permission-mode", "bypassPermissions"],
        ["--setting-sources", ""],
        ["--tools", "Read,Bash"],
        ["--max-turns", "7"],
    ):
        i = cmd.index(pair[0])
        assert cmd[i : i + 2] == pair
    for flag in ("--verbose", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in cmd
    assert "--resume" not in cmd
    assert "--bare" not in cmd


def test_build_command_resume_swaps_session_flag(tmp_path):
    req = make_request(tmp_path, resume=True)
    cmd = agent_cc.build_command(req)
    i = cmd.index("--resume")
    assert cmd[i + 1] == req.session_id
    assert "--session-id" not in cmd


def test_settings_carry_guard_hooks_and_always_thinking(tmp_path):
    req = make_request(tmp_path, guards=["/g/a.sh", "/g/b.sh"])
    cmd = agent_cc.build_command(req)
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    assert settings["alwaysThinkingEnabled"] is True
    assert "disableAllHooks" not in settings  # it would silence the guard (spec §4)
    [entry] = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "*"
    assert [h["command"] for h in entry["hooks"]] == ["/g/a.sh", "/g/b.sh"]
    assert all(h["type"] == "command" for h in entry["hooks"])


def test_settings_without_guards_have_no_hooks_key(tmp_path):
    cmd = agent_cc.build_command(make_request(tmp_path))
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    assert "hooks" not in settings
    assert settings["alwaysThinkingEnabled"] is True


def test_no_attachments_leaves_the_prompt_untouched(tmp_path):
    req = make_request(tmp_path)
    assert agent_cc.build_command(req)[2] == req.prompt


def test_attachments_are_named_in_a_line_before_the_prompt(tmp_path):
    req = make_request(tmp_path, attachments=["/refs/a.png", "/refs/b.svg"])
    prompt = agent_cc.build_command(req)[2]
    assert prompt.startswith("Reference files to read first: /refs/a.png, /refs/b.svg\n\n")
    assert prompt.endswith(req.prompt)


def test_no_tools_means_no_tools_flag(tmp_path):
    assert "--tools" not in agent_cc.build_command(make_request(tmp_path))


def test_empty_tools_list_disables_every_tool(tmp_path):
    cmd = agent_cc.build_command(make_request(tmp_path, tools=[]))
    i = cmd.index("--tools")
    assert cmd[i : i + 2] == ["--tools", ""]


@pytest.mark.parametrize(
    "thinking,expected",
    [("off", "low"), ("minimal", "low"), ("low", "low"), ("medium", "medium"),
     ("high", "high"), ("xhigh", "xhigh"), ("max", "max")],
)
def test_thinking_to_effort(thinking, expected):
    assert agent_cc.effort(thinking) == expected


def test_replayed_ok(tmp_path, fake_cc):
    holder = fake_cc(FakeProc(to_lines(ok_events()), returncode=0))
    events, pids, exits = [], [], []
    req = make_request(tmp_path)
    result = agent_cc.run(req, on_event=events.append, on_spawn=pids.append, on_exit=exits.append)

    assert result.status == "ok"
    assert result.text == "pong"
    assert result.session_id == "cc-1"
    assert result.returncode == 0
    assert result.usage.input == 4
    assert result.usage.output == 3
    assert result.usage.cache_read == 100
    assert result.usage.cache_write == 4500
    assert result.usage.reasoning == 2
    assert result.cost == pytest.approx(0.0123)
    assert result.context_tokens == 4604  # input + cache_read + cache_write, output excluded
    assert result.context_window == 200000
    assert result.denials == []
    assert pids == [4242] and exits == [holder["proc"].pid]
    assert holder["kwargs"]["cwd"] == req.cwd
    assert holder["kwargs"]["stdin"] == agent_cc.DEVNULL


@pytest.mark.parametrize("case", ["ok", "nonzero_error", "timeout"])
def test_exit_callback_gets_the_pid_regardless_of_outcome(tmp_path, fake_cc, case):
    """The exit callback is dispatched with the process's pid — not its return code —
    whatever the outcome, while `AgentResult.returncode` stays the real return code."""
    if case == "ok":
        proc = FakeProc(to_lines(ok_events()), returncode=0)
        req = make_request(tmp_path)
    elif case == "nonzero_error":
        proc = FakeProc([], returncode=7, stderr="Error: boom\n")
        req = make_request(tmp_path)
    else:
        proc = FakeProc(to_lines([init_event()]), returncode=1, hang=True, dies_on=signal.SIGTERM)
        req = make_request(tmp_path, timeout_s=1)
    fake_cc(proc)
    pids, exits = [], []
    result = agent_cc.run(req, on_spawn=pids.append, on_exit=exits.append)

    assert pids == [proc.pid]
    assert exits == [proc.pid]  # the pid, never the return code
    assert result.returncode == proc.returncode


def test_exit_closes_child_process_before_parent(tmp_path, fake_cc, monkeypatch):
    """The child's process row is ended as soon as `agent_cc.run()` returns, while the
    parent Run is still open; a later parent close cannot rewrite that first end time,
    and a repeated end delivery is harmless."""
    clock = {"t": "2020-01-01T00:00:00"}
    monkeypatch.setattr(tracer, "_now", lambda: clock["t"])
    tr = tracer.Tracer(tmp_path / "runs")
    tr.start_run("R1", workflow="feature", request="x")

    def on_spawn(pid):
        tr.start_process("R1", kind="agent", name="builder", pid=pid, command="claude")

    def on_exit(pid):
        tr.end_process("R1", pid)

    fake_cc(FakeProc(to_lines(ok_events()), returncode=0))
    agent_cc.run(make_request(tmp_path), on_spawn=on_spawn, on_exit=on_exit)

    row = tr.con.execute(
        "SELECT pid, ended_at FROM processes WHERE run_id='R1'"
    ).fetchone()
    assert row["pid"] == 4242
    assert row["ended_at"] == clock["t"]
    # the child is already ended while the parent Run is still running
    assert tr.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()["status"] == "running"

    # repeated end callback delivery is harmless and does not move the first end time
    tr.end_process("R1", 4242)
    assert tr.con.execute("SELECT ended_at FROM processes WHERE run_id='R1'").fetchone()["ended_at"] == clock["t"]

    # a later parent close must not rewrite the child's first end timestamp
    clock["t"] = "2020-01-02T00:00:00"
    tr.set_run_status("R1", "success")
    assert tr.con.execute(
        "SELECT ended_at FROM processes WHERE run_id='R1'"
    ).fetchone()["ended_at"] == "2020-01-01T00:00:00"


def test_process_writer_identity_guards():
    """Source guard: cc dispatches its exit callback with `proc.pid`; the tracer's
    process writers create with `start_process`, end a specific pid, and never rewrite
    an already-ended row on the parent-close fallback."""
    source = Path(agent_cc.__file__).read_text()
    exit_calls = [
        c for c in ast.walk(ast.parse(source))
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "on_exit"
    ]
    assert len(exit_calls) == 1, "cc dispatches its exit callback exactly once"
    assert ast.get_source_segment(source, exit_calls[0].args[0]) == "proc.pid"

    tracer_src = Path(tracer.__file__).read_text()
    sqls = [
        n.value for n in ast.walk(ast.parse(tracer_src))
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "processes" in n.value
    ]
    joined = "\n".join(sqls)
    assert "INSERT INTO processes" in joined  # creation via start_process
    assert "UPDATE processes SET ended_at=? WHERE run_id=? AND pid=? AND ended_at IS NULL" in joined
    assert "UPDATE processes SET ended_at=? WHERE run_id=? AND ended_at IS NULL" in joined


def test_replayed_ok_writes_raw_and_emits_turn(tmp_path, fake_cc):
    lines = to_lines(ok_events())
    fake_cc(FakeProc(lines, returncode=0))
    events = []
    req = make_request(tmp_path)
    agent_cc.run(req, on_event=events.append)

    assert open(req.raw_output_path).readlines() == lines
    turns = [e for e in events if e["type"] == "turn"]
    assert len(turns) == 1
    assert turns[0]["model"] == MODEL
    assert turns[0]["context_tokens"] == 4604


def test_duplicate_assistant_events_for_one_message_count_once(tmp_path, fake_cc):
    # stream-json repeats the message (same id) per content block, usage attached to each
    events = [
        init_event(),
        assistant_event(text="", message_id="msg_1"),
        assistant_event(text="pong", message_id="msg_1"),
        assistant_event(text="final", message_id="msg_2", input=10, cache_read=0, cache_write=5000),
        result_event(text="final", input=14, cache_read=100, cache_write=9500),  # run-cumulative sums
    ]
    fake_cc(FakeProc(to_lines(events), returncode=0))
    seen = []
    result = agent_cc.run(make_request(tmp_path), on_event=seen.append)
    assert result.usage.input == 14  # msg_1 once + msg_2, not msg_1 twice
    assert result.context_tokens == 5010  # last assistant message wins, not result.usage
    assert result.text == "final"
    assert len([e for e in seen if e["type"] == "turn"]) == 2


def test_user_line_tool_results_become_one_event_each():
    seen = []
    stream = agent_cc._Stream(MODEL, seen.append)
    line = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok out"},
                {
                    "type": "tool_result",
                    "tool_use_id": "t2",
                    "is_error": True,
                    "content": [
                        {"type": "text", "text": "a"},
                        {"type": "image", "source": {}},
                        {"type": "text", "text": "b"},
                    ],
                },
                {"type": "text", "text": "not a result"},
            ]
        },
        "tool_use_result": {},
    }
    stream.feed(json.dumps(line))
    assert seen == [
        line,
        {"type": "tool_result", "tool_call_id": "t1", "tool": "", "ok": True, "output": "ok out"},
        {"type": "tool_result", "tool_call_id": "t2", "tool": "", "ok": False, "output": "a\nb"},
    ]


def test_a_user_line_with_string_content_emits_no_result():
    seen = []
    stream = agent_cc._Stream(MODEL, seen.append)
    line = {"type": "user", "message": {"content": "just a string"}}
    stream.feed(json.dumps(line))
    assert seen == [line]


def test_context_window_matches_the_requested_model(tmp_path, fake_cc):
    # subagent models get their own modelUsage rows — the requested model's window wins
    final = result_event()
    final["modelUsage"] = {
        "claude-fable-5": {"contextWindow": 1000000},
        "claude-haiku-4-5-20251001": {"contextWindow": 200000},
    }
    fake_cc(FakeProc(to_lines([init_event(), assistant_event(), final]), returncode=0))
    assert agent_cc.run(make_request(tmp_path)).context_window == 200000


def test_replayed_denied(tmp_path, fake_cc):
    denial = {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "rm -rf /"}}
    events = [init_event(), assistant_event(), result_event(denials=[denial])]
    fake_cc(FakeProc(to_lines(events), returncode=0))
    result = agent_cc.run(make_request(tmp_path))
    assert result.status == "denied"
    assert result.denials == [denial]


def report_text(status, fenced=True):
    body = json.dumps(
        {"status": status, "summary": f"{status} report", "artifacts": [], "notes_for_next_agent": ""}
    )
    return f"```json\n{body}\n```" if fenced else body


def _cc_warnings(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "lime.agent_cc" and r.levelno == logging.WARNING
    ]


@pytest.mark.parametrize("status", ["success", "partial", "fail"])
@pytest.mark.parametrize("fenced", [True, False], ids=["fenced", "bare"])
def test_late_denial_keeps_valid_envelope(tmp_path, fake_cc, caplog, status, fenced):
    caplog.set_level(logging.WARNING, logger="lime.agent_cc")
    report = report_text(status, fenced)
    denials = [
        {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "rm -rf /"}},
        {"tool_name": "Read", "tool_use_id": "t2", "tool_input": {"file_path": "/x"}},
    ]
    events = [init_event(), assistant_event(text=report), result_event(text=report, denials=denials)]
    fake_cc(FakeProc(to_lines(events), returncode=0))
    result = agent_cc.run(make_request(tmp_path))

    assert result.status == "ok"
    assert result.text == report  # exact text preserved for the role-specific parser
    assert extract_envelope(result.text, GenericOutput).status == status
    assert result.denials == denials
    warnings = _cc_warnings(caplog)
    assert len(warnings) == 2  # one per denied tool, no more
    assert any("Bash" in m for m in warnings)
    assert any("Read" in m for m in warnings)


@pytest.mark.parametrize(
    "text",
    ["", "no report here", '{"status": "success"', '{"foo": "bar"}', '{"status": "complete"}'],
    ids=["empty", "plain", "malformed", "no_status", "bad_status"],
)
def test_late_denial_without_valid_envelope_stays_denied(tmp_path, fake_cc, caplog, text):
    caplog.set_level(logging.WARNING, logger="lime.agent_cc")
    denials = [{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "rm -rf /"}}]
    events = [init_event(), assistant_event(text=text), result_event(text=text, denials=denials)]
    fake_cc(FakeProc(to_lines(events), returncode=0))
    result = agent_cc.run(make_request(tmp_path))

    assert result.status == "denied"
    assert result.denials == denials
    assert _cc_warnings(caplog) == []  # no report won, so no ignored-denial warning


def test_late_denial_does_not_leak_between_runs(tmp_path, fake_cc, caplog):
    caplog.set_level(logging.WARNING, logger="lime.agent_cc")
    denials = [{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "ls"}}]
    report = report_text("success")
    fake_cc(FakeProc(to_lines([init_event(), assistant_event(text=report), result_event(text=report, denials=denials)]), returncode=0))
    assert agent_cc.run(make_request(tmp_path)).status == "ok"
    assert len(_cc_warnings(caplog)) == 1

    caplog.clear()
    # a later run with no report cannot inherit the earlier exemption or its warnings
    fake_cc(FakeProc(to_lines([init_event(), assistant_event(text="pong"), result_event(text="pong", denials=denials)]), returncode=0))
    second = agent_cc.run(make_request(tmp_path))
    assert second.status == "denied"
    assert _cc_warnings(caplog) == []


def test_replayed_max_turns(tmp_path, fake_cc):
    events = [init_event(), assistant_event(), result_event(subtype="error_max_turns", is_error=True, text="")]
    fake_cc(FakeProc(to_lines(events), returncode=1))
    result = agent_cc.run(make_request(tmp_path, max_turns=1))
    assert result.status == "max_turns"


def test_replayed_run_error(tmp_path, fake_cc):
    events = [init_event(), result_event(subtype="error_during_execution", is_error=True, text="boom")]
    fake_cc(FakeProc(to_lines(events), returncode=1))
    assert agent_cc.run(make_request(tmp_path)).status == "error"


def test_startup_error_keeps_stderr_tail(tmp_path, fake_cc):
    # bad flag/auth: stderr only, no JSON stream at all (gotcha 3)
    fake_cc(FakeProc([], returncode=1, stderr="Error: Invalid model name\n"))
    result = agent_cc.run(make_request(tmp_path))
    assert result.status == "error"
    assert "Invalid model" in result.stderr_tail
    assert result.session_id == ""


def test_timeout_sends_sigint_before_sigterm(tmp_path, fake_cc):
    proc = FakeProc(to_lines([init_event()]), returncode=1, hang=True, dies_on=signal.SIGTERM)
    fake_cc(proc)
    result = agent_cc.run(make_request(tmp_path, timeout_s=1))
    assert result.status == "timeout"
    assert proc.signals == [signal.SIGINT, signal.SIGTERM]


def test_signal_ladder_is_shared_with_pi():
    """CC stops its child through pi's ladder — one implementation, not a duplicate."""
    assert agent_cc._signal_ladder is agent_pi._signal_ladder


@pytest.mark.parametrize(
    "dies_on,expected",
    [
        (signal.SIGINT, [signal.SIGINT]),
        (signal.SIGTERM, [signal.SIGINT, signal.SIGTERM]),
        (signal.SIGKILL, [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]),
    ],
    ids=["sigint", "sigterm", "sigkill"],
)
def test_shared_signal_ladder_escalation(short_grace, dies_on, expected):
    """The shared ladder escalates SIGINT → SIGTERM → SIGKILL, stopping at the first
    wait that succeeds and killing only after both graces expire."""
    proc = FakeProc([], hang=True, dies_on=dies_on)
    agent_cc._signal_ladder(proc)
    assert proc.signals == expected


class _ExitedProc:
    """A process that already vanished: signals/waits race with ProcessLookupError."""

    def __init__(self):
        self.signals: list[int] = []

    def send_signal(self, sig):
        self.signals.append(sig)
        raise ProcessLookupError

    def wait(self, timeout=None):
        raise TimeoutExpired(cmd="claude", timeout=timeout or 0)

    def kill(self):
        self.signals.append(signal.SIGKILL)
        raise ProcessLookupError


def test_shared_signal_ladder_tolerates_exited_process(short_grace):
    """A process that vanished mid-stop must not surface the missing-process race: the
    ladder swallows ProcessLookupError/TimeoutExpired and still walks the sequence."""
    proc = _ExitedProc()
    agent_cc._signal_ladder(proc)
    assert proc.signals == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]


def test_tolerates_non_json_lines(tmp_path, fake_cc):
    lines = ["not json at all\n", *to_lines(ok_events()), "\n"]
    fake_cc(FakeProc(lines, returncode=0))
    req = make_request(tmp_path)
    result = agent_cc.run(req)
    assert result.status == "ok"
    assert open(req.raw_output_path).readlines() == lines


def test_child_env_marks_and_thinking(tmp_path, fake_cc, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    holder = fake_cc(FakeProc(to_lines(ok_events()), returncode=0))
    agent_cc.run(make_request(tmp_path))
    env = holder["kwargs"]["env"]
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env
    assert env["CLAUDE_CODE_DISABLE_ADAPTATIVE_THINKING"] == "1"


@pytest.mark.parametrize("writes", [None, [], ["src/app.py"]], ids=["unset", "readonly", "allowlist"])
@pytest.mark.parametrize("inherited", [True, False], ids=["inherited", "absent"])
def test_child_env_exports_writes(tmp_path, fake_cc, monkeypatch, writes, inherited):
    """The request's writes list round-trips as LIME_WRITES; None removes any
    inherited policy so stale policy never leaks across roles or sessions."""
    if inherited:
        monkeypatch.setenv("LIME_WRITES", json.dumps(["stale/"]))
    else:
        monkeypatch.delenv("LIME_WRITES", raising=False)
    holder = fake_cc(FakeProc(to_lines(ok_events()), returncode=0))
    agent_cc.run(make_request(tmp_path, writes=writes))
    env = holder["kwargs"]["env"]
    if writes is None:
        assert "LIME_WRITES" not in env
    else:
        assert json.loads(env["LIME_WRITES"]) == writes


def test_settings_guard_hook_is_a_direct_command_without_error_wrapper(tmp_path):
    """A failed hook must keep Claude Code's native nonblocking error behavior:
    no error-to-deny shell wrapper (`||`/exit 2) and no disabled hooks."""
    req = make_request(tmp_path, guards=["lime guard"])
    cmd = agent_cc.build_command(req)
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    assert "disableAllHooks" not in settings
    [entry] = settings["hooks"]["PreToolUse"]
    [hook] = entry["hooks"]
    assert hook["type"] == "command"
    assert hook["command"] == "lime guard"
    assert "||" not in hook["command"]
    assert "exit 2" not in hook["command"]


ENVELOPE_SYSTEM = (
    "You are a probe agent. Whatever else you do, end your reply with ONLY a fenced ```json block matching "
    '{"status": "success", "summary": "<one sentence>", "artifacts": [], '
    '"notes_for_next_agent": ""} — no prose outside the fence.'
)


@pytest.mark.real
def test_real_call_envelope_guard_hook_and_resume(tmp_path):
    marker = tmp_path / "guard-fired"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    hook.chmod(0o755)

    req = make_request(
        tmp_path,
        system_prompt=ENVELOPE_SYSTEM,
        prompt="Remember the word 'limequat'. Run `echo ping` with Bash, then report what you did.",
        tools=["Bash"],
        guards=[str(hook)],
        timeout_s=180,
    )
    events = []
    result = agent_cc.run(req, on_event=events.append)

    assert result.status == "ok", result.stderr_tail
    # A real, subscription-billed call: when this environment's Claude Code
    # subscription access is disabled the child answers 0 with an auth notice
    # instead of an envelope, so the prerequisite is simply absent — skip
    # rather than fail the suite on an outside blocker.
    if "disabled Claude subscription access" in result.text or "Not logged in" in result.text:
        pytest.skip("Claude Code subscription access is unavailable in this environment")
    envelope = extract_envelope(result.text, GenericOutput)
    assert envelope.status == "success"
    init = next(e for e in events if e.get("type") == "system" and e.get("subtype") == "init")
    assert init["apiKeySource"] == "none"  # subscription kept
    assert marker.exists()  # the injected PreToolUse guard hook fired
    assert result.session_id == req.session_id
    assert 0 < result.context_tokens < 10_000  # the probed lean floor — default loading would be ~22k
    assert result.context_window > 0
    assert any(e["type"] == "turn" for e in events)

    req2 = make_request(
        tmp_path,
        session_id=req.session_id,
        resume=True,
        system_prompt=ENVELOPE_SYSTEM,
        prompt="What word did I ask you to remember? Put it in the summary.",
        tools=["Bash"],
        raw_output_path=str(tmp_path / "raw_output_2.jsonl"),
        timeout_s=180,
    )
    result2 = agent_cc.run(req2)
    assert result2.status == "ok", result2.stderr_tail
    envelope2 = extract_envelope(result2.text, GenericOutput)
    assert "limequat" in envelope2.summary.lower()  # resume re-entered the same session


def test_should_stop_ends_the_run_as_timeout(tmp_path, fake_cc):
    """The orchestrator's per-Session stop (ticket 173) ends the child like the time
    ceiling with exactly one bounded SIGINT ladder (the `not stopped["timeout"]` guard
    keeps it to one)."""
    proc = FakeProc(to_lines([init_event(), assistant_event()]), returncode=1, hang=True)
    fake_cc(proc)
    result = agent_cc.run(make_request(tmp_path, should_stop=lambda: True))

    assert result.status == "timeout"
    assert proc.signals == [signal.SIGINT]


def test_child_env_sets_runtime_for_guard(tmp_path, fake_cc, monkeypatch):
    """The guard selects its partial-delivery verb by runtime: the adapter forces its own
    marker before spawning, overriding any inherited one."""
    monkeypatch.setenv("LIME_AGENT_RUNTIME", "pi")  # opposite of this adapter's runtime
    holder = fake_cc(FakeProc(to_lines(ok_events()), returncode=0))
    agent_cc.run(make_request(tmp_path))
    assert holder["kwargs"]["env"]["LIME_AGENT_RUNTIME"] == "claude_code"
