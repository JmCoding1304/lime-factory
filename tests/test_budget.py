"""Context budget: thresholds, budget.json, and the handoff note's shape (spec §9)."""

import subprocess

import pytest

from lime import budget


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


# --- thresholds ---


def test_stop_threshold_caps_the_default_when_the_window_is_small():
    assert budget.stop_threshold(200_000) == 160_000  # 200k - 2*20k reserve, below the 250k default


def test_stop_threshold_keeps_the_default_when_the_window_is_generous():
    assert budget.stop_threshold(1_000_000) == 250_000  # 1M - 20k is still above the default


def test_stop_threshold_falls_back_to_the_default_for_an_unknown_window():
    assert budget.stop_threshold(0) == 250_000


def test_backstop_is_window_minus_reserve():
    assert budget.backstop(200_000) == 180_000


def test_backstop_never_fires_early_for_an_unknown_window():
    assert budget.backstop(0) > budget.STOP  # any real context_tokens count stays under it


def test_thresholds_prefers_the_agent_override_over_the_run_config():
    warn, stop, kill_at = budget.thresholds({"warn": 10, "stop": 20}, {"warn": 100, "stop": 200}, 0)
    assert (warn, stop) == (10, 20)


def test_thresholds_falls_back_to_the_run_config_then_the_default():
    warn, stop, _ = budget.thresholds(None, {"warn": 100}, 0)
    assert warn == 100
    assert stop == budget.STOP  # run config didn't set stop either


@pytest.mark.parametrize("window", [0, 64_000, 200_000, 1_000_000])
@pytest.mark.parametrize("override", [None, 190_000, 400_000])
def test_stop_sits_at_least_one_reserve_below_the_backstop(window, override):
    _, stop, kill_at = budget.thresholds({"stop": override} if override else None, None, window)
    assert kill_at - stop >= budget.RESERVE


def test_thresholds_applies_the_window_cap_to_the_resolved_stop():
    _, stop, kill_at = budget.thresholds(None, None, 200_000)
    assert stop == 160_000
    assert kill_at == 180_000


# --- evaluate ---


def test_evaluate_ok_below_warn():
    assert budget.evaluate(100, warn=150, stop=250) == "ok"


def test_evaluate_warn_between_warn_and_stop():
    assert budget.evaluate(150, warn=150, stop=250) == "warn"
    assert budget.evaluate(200, warn=150, stop=250) == "warn"


def test_evaluate_stop_at_or_above_stop():
    assert budget.evaluate(250, warn=150, stop=250) == "stop"
    assert budget.evaluate(999, warn=150, stop=250) == "stop"


# --- budget.json ---


def test_write_then_read_round_trips(tmp_path):
    path = tmp_path / "budget.json"
    budget.write(path, state="warn", context_tokens=160_000, warn=150_000, stop=250_000)
    assert budget.read(path) == {
        "state": "warn", "context_tokens": 160_000, "warn": 150_000, "stop": 250_000,
    }


def test_write_round_trips_time_limit_reason(tmp_path):
    path = tmp_path / "budget.json"
    budget.write(path, state="stop", context_tokens=4_321, warn=150_000, stop=250_000,
                 reason="time_limit")
    assert budget.read(path) == {
        "state": "stop", "context_tokens": 4_321, "warn": 150_000, "stop": 250_000,
        "reason": "time_limit",
    }


def test_write_without_reason_clears_previous_reason(tmp_path):
    path = tmp_path / "budget.json"
    budget.write(path, state="stop", context_tokens=10, warn=150_000, stop=250_000,
                 reason="time_limit")
    assert budget.read(path)["reason"] == "time_limit"
    budget.write(path, state="ok", context_tokens=0, warn=150_000, stop=250_000)
    assert budget.read(path) == {
        "state": "ok", "context_tokens": 0, "warn": 150_000, "stop": 250_000,
    }


def test_read_missing_file_is_ok(tmp_path):
    assert budget.read(tmp_path / "nope.json") == {"state": "ok"}


def test_read_corrupt_file_is_ok(tmp_path):
    path = tmp_path / "budget.json"
    path.write_text("not json")
    assert budget.read(path) == {"state": "ok"}


# --- handoff note validation ---


def test_handoff_sections_stay_and_the_shape_check_is_gone():
    assert budget.HANDOFF_SECTIONS == ("Done", "Left", "Decisions", "Don't redo", "Open")
    assert not hasattr(budget, "check_handoff")
    assert not hasattr(budget, "HANDOFF_MAX_LINES")


# --- git facts and the code-only note ---


def test_git_facts_reports_status_and_diff_stat(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("v1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    (tmp_path / "a.txt").write_text("v2\n")
    (tmp_path / "b.txt").write_text("new\n")
    monkeypatch.chdir(tmp_path)

    facts = budget.git_facts(tmp_path, 1)
    assert "continuation: 1" in facts
    assert "b.txt" in facts  # git status --short
    assert "a.txt" in facts  # git diff --stat


def test_code_only_note_has_all_five_sections_and_git_facts(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("v1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    monkeypatch.chdir(tmp_path)

    note = budget.code_only_note(tmp_path, 1)
    for section in budget.HANDOFF_SECTIONS:
        assert f"## {section}" in note
        assert "backstop kill" in note
    assert "## Git facts" in note


def test_code_only_note_names_its_cause(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("v1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    monkeypatch.chdir(tmp_path)

    note = budget.code_only_note(tmp_path, 1, cause="time ceiling kill")
    for section in budget.HANDOFF_SECTIONS:
        assert "time ceiling kill" in note
        assert "backstop kill" not in note
