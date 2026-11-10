"""Tests for lime/home.py — the home repo (`lime dash init`, ticket 218)."""

from __future__ import annotations

import fcntl
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from lime import home

T0 = datetime(2026, 11, 5, 9, 0, tzinfo=timezone(timedelta(hours=-3)))


def _cp(args, rc=0, out="", err=""):
    return subprocess.CompletedProcess(list(args), rc, out, err)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def log(repo):
    return _git(repo, "log", "--format=%s").stdout.splitlines()


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now


class FakeGh:
    def __init__(self, remote_url):
        self.calls = []
        self.authed = True
        self.login = "octo"
        self.create_rc = 0
        self.remote_url = remote_url
        self.gh_missing = False

    def __call__(self, *args):
        self.calls.append(tuple(args))
        if args[:2] == ("auth", "status"):
            if self.gh_missing:
                return _cp(args, 127, "", "gh not found")
            return _cp(args, 0 if self.authed else 1, "", "" if self.authed else "not logged in")
        if args[:2] == ("api", "user"):
            return _cp(args, 0, self.login + "\n", "")
        if args[:2] == ("repo", "create"):
            if self.create_rc != 0:
                return _cp(args, self.create_rc, "", "HTTP 422: name already exists")
            source = args[args.index("--source") + 1]
            remote = Path(self.remote_url)
            if not remote.exists():
                subprocess.run(
                    ["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True
                )
            subprocess.run(
                ["git", "-C", source, "remote", "add", "origin", self.remote_url],
                check=True,
            )
            return _cp(args, 0, "", "")
        raise AssertionError(f"unexpected gh call: {args}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    home_root = tmp_path / "home"
    home_root.mkdir()
    monkeypatch.setenv("HOME", str(home_root))
    monkeypatch.delenv("LIME_AGENT_RUNTIME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    (tmp_path / "gitconfig").write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "t")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "t@t")

    clock = Clock(T0)
    monkeypatch.setattr(home, "_now", clock)
    remote = str(tmp_path / "remote.git")
    gh = FakeGh(remote)
    monkeypatch.setattr(home, "_gh", gh)
    return SimpleNamespace(
        tmp=tmp_path,
        home=home_root / ".lime" / "dashboard",
        remote=Path(remote),
        gh=gh,
        clock=clock,
    )


def test_init_prints_account_then_creates_repo_files_commit_and_private_origin(env, capsys):
    assert home.init_command() == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "dashboard: gh account octo"
    assert any(line.startswith("dashboard: home repo ready at") for line in out)
    h = env.home
    assert (h / "focus.md").read_text() == "# Focus\n"
    assert (h / "last-visit").read_text() == home._visit_text(T0, T0)
    assert (h / ".gitignore").read_text().splitlines() == ["push-error", ".lock"]
    assert log(h) == ["dashboard init"]
    assert _git(h, "branch", "--show-current").stdout.strip() == "main"
    assert (
        "repo", "create", "octo/lime-dashboard", "--private",
        "--source", str(h), "--remote", "origin",
    ) in env.gh.calls
    assert _git(env.remote, "rev-parse", "main").stdout.strip() == _git(
        h, "rev-parse", "HEAD"
    ).stdout.strip()
    assert _git(h, "status", "--porcelain").stdout == ""
    assert not (h / "push-error").exists()


def test_second_init_prints_already_set_up_and_changes_nothing(env, capsys):
    assert home.init_command() == 0
    assert home.init_command() == 0
    assert f"dashboard: already set up (origin = {env.gh.remote_url})" in capsys.readouterr().out
    assert log(env.home) == ["dashboard init"]
    assert sum(1 for c in env.gh.calls if c[:2] == ("repo", "create")) == 1


@pytest.mark.parametrize("missing", [False, True])
def test_init_without_gh_auth_exits_2_names_gh_auth_login_and_creates_nothing(
    env, capsys, missing
):
    if missing:
        env.gh.gh_missing = True
    else:
        env.gh.authed = False
    assert home.init_command() == 2
    assert "gh auth login" in capsys.readouterr().err
    assert not (env.tmp / "home" / ".lime").exists()
    assert not any(c[:2] == ("repo", "create") for c in env.gh.calls)


@pytest.mark.parametrize("value", ["pi", ""])
def test_init_refuses_inside_a_role_session(env, monkeypatch, capsys, value):
    monkeypatch.setenv("LIME_AGENT_RUNTIME", value)
    assert home.init_command() == 2
    assert "lime dash init: owner-only; refused inside a Role session" in capsys.readouterr().err
    assert env.gh.calls == []
    assert not (env.tmp / "home" / ".lime").exists()


def test_failed_repo_create_exits_1_and_a_rerun_finishes(env, capsys):
    env.gh.create_rc = 1
    assert home.init_command() == 1
    err = capsys.readouterr().err
    assert "gh repo create octo/lime-dashboard failed" in err
    assert "HTTP 422: name already exists" in err
    h = env.home
    assert log(h) == ["dashboard init"]
    assert _git(h, "remote", "get-url", "origin").returncode != 0

    env.gh.create_rc = 0
    assert home.init_command() == 0
    assert log(h) == ["dashboard init"]
    assert _git(h, "remote", "get-url", "origin").returncode == 0
    assert _git(env.remote, "rev-parse", "main").stdout.strip() == _git(
        h, "rev-parse", "HEAD"
    ).stdout.strip()


def test_lock_is_an_exclusive_flock_on_dot_lock_and_never_nests(env):
    h = env.home
    with home.locked(h):
        fd = open(h / ".lock", "a")
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            fd.close()
        with pytest.raises(RuntimeError):
            with home.locked(h):
                pass
    fd = open(h / ".lock", "a")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


def test_write_outside_the_lock_is_refused(env):
    assert home.init_command() == 0
    called = []
    with pytest.raises(RuntimeError):
        home.write(env.home, "x", lambda p: called.append(p))
    assert called == []
    assert log(env.home) == ["dashboard init"]


def test_write_commits_a_hand_edit_first_then_one_commit_then_pushes(env):
    assert home.init_command() == 0
    h = env.home
    (h / "focus.md").write_text((h / "focus.md").read_text() + "- hand row\n")
    with home.locked(h):
        home.write(
            h, "test: write",
            lambda p: (p / "focus.md").write_text((p / "focus.md").read_text() + "- applied\n"),
        )
    assert log(h) == ["test: write", "focus: hand edit", "dashboard init"]
    assert _git(h, "status", "--porcelain").stdout == ""
    assert _git(env.remote, "rev-parse", "main").stdout.strip() == _git(
        h, "rev-parse", "HEAD"
    ).stdout.strip()


def test_failed_push_keeps_the_commit_then_the_next_write_pushes_both(env, capsys):
    assert home.init_command() == 0
    h = env.home
    _git(h, "remote", "set-url", "origin", str(env.tmp / "missing.git"))
    with home.locked(h):
        home.write(
            h, "w1",
            lambda p: (p / "focus.md").write_text((p / "focus.md").read_text() + "a\n"),
        )
    assert log(h)[0] == "w1"
    assert (h / "push-error").read_text().strip() != ""
    lines = [line for line in capsys.readouterr().err.splitlines() if "push failed" in line]
    assert len(lines) == 1
    assert lines[0].startswith("dashboard: push failed (")
    assert lines[0].endswith("· 1 commits not pushed")

    _git(h, "remote", "set-url", "origin", env.gh.remote_url)
    with home.locked(h):
        home.write(
            h, "w2",
            lambda p: (p / "focus.md").write_text((p / "focus.md").read_text() + "b\n"),
        )
    assert "push failed" not in capsys.readouterr().err
    assert not (h / "push-error").exists()
    assert _git(env.remote, "rev-list", "--count", "main").stdout.strip() == "3"


def test_push_has_no_terminal_prompt_and_a_timeout(env, monkeypatch):
    real_run = home.subprocess.run
    recorded = []

    def spy(cmd, **kwargs):
        if "push" in cmd:
            recorded.append((cmd, kwargs))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(home.subprocess, "run", spy)
    assert home.init_command() == 0
    assert recorded
    _, kwargs = recorded[-1]
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert kwargs["timeout"] == home.PUSH_TIMEOUT_S


def test_push_timeout_is_reported_and_keeps_the_commit(env, monkeypatch, capsys):
    assert home.init_command() == 0
    h = env.home
    real_run = home.subprocess.run

    def spy(cmd, **kwargs):
        if "push" in cmd:
            raise subprocess.TimeoutExpired(cmd, home.PUSH_TIMEOUT_S)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(home.subprocess, "run", spy)
    with home.locked(h):
        home.write(
            h, "w1",
            lambda p: (p / "focus.md").write_text((p / "focus.md").read_text() + "a\n"),
        )
    assert log(h)[0] == "w1"
    assert "push failed (timed out after 20s) · 1 commits not pushed" in capsys.readouterr().err
    assert (h / "push-error").exists()


def test_first_visit_after_init_prints_equal_stamps_and_writes_nothing(env, capsys):
    assert home.init_command() == 0
    capsys.readouterr()
    h = env.home
    before = (h / "last-visit").read_text()
    env.clock.now = T0 + timedelta(minutes=5)
    assert home.visit_command() == 0
    assert capsys.readouterr().out == home._visit_text(T0, T0)
    assert log(h) == ["dashboard init"]
    assert (h / "last-visit").read_text() == before


def test_visit_exactly_at_the_gap_writes_nothing(env, capsys):
    assert home.init_command() == 0
    capsys.readouterr()
    h = env.home
    env.clock.now = T0 + timedelta(minutes=30)
    assert home.visit_command() == 0
    assert capsys.readouterr().out == home._visit_text(T0, T0)
    assert log(h) == ["dashboard init"]
    assert (h / "last-visit").read_text() == home._visit_text(T0, T0)


def test_visit_after_the_gap_moves_previous_and_commits_and_pushes(env, capsys):
    assert home.init_command() == 0
    capsys.readouterr()
    h = env.home
    t1 = T0 + timedelta(minutes=31)
    env.clock.now = t1
    assert home.visit_command() == 0
    assert capsys.readouterr().out == home._visit_text(T0, t1)
    assert log(h)[0] == f"visit {home._stamp(t1)}"
    assert (h / "last-visit").read_text() == home._visit_text(T0, t1)
    assert _git(env.remote, "rev-parse", "main").stdout.strip() == _git(
        h, "rev-parse", "HEAD"
    ).stdout.strip()

    env.clock.now = t1 + timedelta(minutes=1)
    assert home.visit_command() == 0
    assert log(h)[0] == f"visit {home._stamp(t1)}"
    assert capsys.readouterr().out == home._visit_text(T0, t1)


def test_visit_commits_a_hand_edit_first(env, capsys):
    assert home.init_command() == 0
    h = env.home
    (h / "focus.md").write_text((h / "focus.md").read_text() + "- hand row\n")
    t1 = T0 + timedelta(minutes=31)
    env.clock.now = t1
    assert home.visit_command() == 0
    assert log(h) == [f"visit {home._stamp(t1)}", "focus: hand edit", "dashboard init"]
    assert _git(h, "status", "--porcelain").stdout == ""


def test_visit_push_failure_then_next_visit_pushes_both(env, capsys):
    assert home.init_command() == 0
    h = env.home
    _git(h, "remote", "set-url", "origin", str(env.tmp / "missing.git"))
    env.clock.now = T0 + timedelta(minutes=31)
    assert home.visit_command() == 0
    err = capsys.readouterr().err
    assert "dashboard: push failed (" in err
    assert "· 1 commits not pushed" in err
    assert (h / "push-error").exists()

    _git(h, "remote", "set-url", "origin", env.gh.remote_url)
    env.clock.now = T0 + timedelta(minutes=62)
    assert home.visit_command() == 0
    assert not (h / "push-error").exists()
    assert _git(env.remote, "rev-list", "--count", "main").stdout.strip() == "3"


def test_visit_before_init_exits_nonzero_naming_init_and_creates_nothing(env, capsys):
    assert home.visit_command() == 1
    assert "run lime dash init" in capsys.readouterr().err
    assert not (env.tmp / "home" / ".lime" / "dashboard").exists()


@pytest.mark.parametrize("value", ["claude_code", ""])
def test_visit_refuses_inside_a_role_session(env, monkeypatch, capsys, value):
    assert home.init_command() == 0
    h = env.home
    before = (h / "last-visit").read_text()
    env.clock.now = T0 + timedelta(minutes=31)
    monkeypatch.setenv("LIME_AGENT_RUNTIME", value)
    assert home.visit_command() == 2
    assert "lime dash visit: owner-only; refused inside a Role session" in capsys.readouterr().err
    assert log(h) == ["dashboard init"]
    assert (h / "last-visit").read_text() == before


def test_unparseable_last_visit_resets_both_stamps(env, capsys):
    assert home.init_command() == 0
    capsys.readouterr()
    h = env.home
    (h / "last-visit").write_text("garbage\n")
    now = T0 + timedelta(minutes=1)
    env.clock.now = now
    assert home.visit_command() == 0
    assert capsys.readouterr().out == home._visit_text(now, now)
    assert log(h)[0] == f"visit {home._stamp(now)}"
    assert log(h)[1] == "focus: hand edit"
