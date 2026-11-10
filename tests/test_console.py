"""Console.finish reporting: plain success/fail rendering in both the printed
panel and the mirrored log event."""

import inspect

import pytest

from lime.console import Console
from lime.tracer import Tracer


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tracer = Tracer(tmp_path / ".lime" / "runs")
    tracer.start_run("R1", workflow="feature", request="do X")
    return tracer, Console(tracer, "R1")


def test_finish_has_no_outcome_parameter_or_handoff_lookup():
    assert "outcome" not in inspect.signature(Console.session_finished).parameters
    assert not hasattr(Console, "_handoff_path")


def test_ordinary_success_and_fail_still_render(console, capsys):
    tracer, con = console
    con.session_finished(True, 10, 0.01, str(tracer.runs_dir / "lime.db"))
    out = capsys.readouterr().out
    assert "✓ success" in out
    assert "needs_rescope" not in out

    tracer2 = Tracer(tracer.runs_dir)
    tracer2.start_run("R2", workflow="feature", request="y")
    Console(tracer2, "R2").session_finished(False, 5, 0.0, str(tracer2.runs_dir / "lime.db"))
    out2 = capsys.readouterr().out
    assert "✗ fail" in out2
    assert "needs_rescope" not in out2
