"""Ticket 218 — the home repo behind `lime dash init`.

Spec: specs/assets/bridge-home-focus.md (read "bridge" as dashboard). One private
Git repo at `~/.lime/dashboard/` holds the cross-project focus file; this module
bootstraps it (`init`), owns its single kernel lock and its single writer.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

REMOTE_NAME = "lime-dashboard"
LOCK_NAME = ".lock"
GITIGNORE_TEXT = "push-error\n.lock\n"
FOCUS_HEADER = "# Focus\n"
VISIT_GAP = timedelta(minutes=30)
PUSH_TIMEOUT_S = 20
GH_TIMEOUT_S = 60
_HELD: set[Path] = set()  # home dirs whose lock this process holds


def home_dir() -> Path:
    """`~/.lime/dashboard/`. Resolved at call time so tests reroute it via `HOME`."""
    return Path.home() / ".lime" / "dashboard"


def _now() -> datetime:
    """The test seam for the current stamp."""
    return datetime.now().astimezone().replace(microsecond=0)


def _stamp(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _visit_text(previous: datetime, current: datetime) -> str:
    """The one formatter of `last-visit`; the visit command reuses it."""
    return f"previous: {_stamp(previous)}\ncurrent: {_stamp(current)}\n"


def owner_only(label: str) -> bool:
    """True inside a Role session, printing the refusal. Callers return 2."""
    if "LIME_AGENT_RUNTIME" in os.environ:
        print(f"{label}: owner-only; refused inside a Role session", file=sys.stderr)
        return True
    return False


def _gh(*args) -> subprocess.CompletedProcess:
    """The only gh seam. A missing gh or a hang becomes a CompletedProcess."""
    try:
        return subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=GH_TIMEOUT_S
        )
    except OSError:
        return subprocess.CompletedProcess(args, 127, "", "gh not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args, 124, "", f"gh timed out after {GH_TIMEOUT_S}s"
        )


def _git(home: Path, *args, check: bool = True):
    """Run one git command in `home`. With `check`, raise on failure and return
    stripped stdout; without it, return the CompletedProcess."""
    r = subprocess.run(["git", "-C", str(home), *args], capture_output=True, text=True)
    if check:
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr}")
        return r.stdout.strip()
    return r


def _has_commit(home: Path) -> bool:
    if not (home / ".git").exists():
        return False
    return _git(home, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0


def _origin_url(home: Path) -> str | None:
    if not (home / ".git").exists():
        return None
    r = _git(home, "remote", "get-url", "origin", check=False)
    if r.returncode != 0:
        return None
    return r.stdout.strip()


@contextlib.contextmanager
def locked(home: Path):
    """Ticket 218 — the home repo's kernel lock.

    Blocking, no timeout. The flock is released when the holder exits, even on a
    crash, so there is never a stale lock to clean. Never nest it: a second fd in
    the same process would self-deadlock. The lock file is created on first use
    and never deleted.
    """
    resolved = home.resolve()
    if resolved in _HELD:
        raise RuntimeError("home lock already held — never nest it")
    home.mkdir(parents=True, exist_ok=True)
    with (home / LOCK_NAME).open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _HELD.add(resolved)
        try:
            yield
        finally:
            _HELD.discard(resolved)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _gh_reason(r: subprocess.CompletedProcess) -> str:
    return _first_line(r.stderr) or f"gh exited {r.returncode}"


def _push_reason(r: subprocess.CompletedProcess) -> str:
    reason = _first_line(r.stderr)
    for prefix in ("fatal: ", "error: "):
        if reason.startswith(prefix):
            reason = reason[len(prefix):]
            break
    return reason or f"git push exited {r.returncode}"


def _pending_count(home: Path) -> str:
    r = _git(home, "rev-list", "--count", "main", "--not", "--remotes=origin", check=False)
    if r.returncode != 0:
        return "0"
    return r.stdout.strip() or "0"


def _report_push_failure(home: Path, reason: str) -> None:
    print(
        f"dashboard: push failed ({reason}) · {_pending_count(home)} commits not pushed",
        file=sys.stderr,
    )


def push(home: Path) -> bool:
    """Best-effort push of `main` to origin. Never raises: a failure keeps the
    commit, records `push-error` and prints one line."""
    argv = ["git", "-C", str(home), "push", "-u", "origin", "main"]
    try:
        r = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            timeout=PUSH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        reason = f"timed out after {PUSH_TIMEOUT_S}s"
        (home / "push-error").write_text(reason + "\n")
        _report_push_failure(home, reason)
        return False
    if r.returncode == 0:
        (home / "push-error").unlink(missing_ok=True)
        return True
    reason = _push_reason(r)
    text = r.stderr or reason
    (home / "push-error").write_text(text if text.endswith("\n") else text + "\n")
    _report_push_failure(home, reason)
    return False


def _read_visit(home: Path) -> tuple[datetime, datetime] | None:
    """Parse `previous:`/`current:` from `last-visit` (first colon only). A
    missing file, a missing key, a naive datetime or a `ValueError` all return
    None."""
    path = home / "last-visit"
    if not path.exists():
        return None
    try:
        values: dict[str, datetime] = {}
        for line in path.read_text().splitlines():
            key, _, value = line.partition(":")
            if key in ("previous", "current"):
                dt = datetime.fromisoformat(value.strip())
                if dt.tzinfo is None:
                    return None
                values[key] = dt
        if "previous" not in values or "current" not in values:
            return None
        return values["previous"], values["current"]
    except ValueError:
        return None


def write(home: Path, message: str, apply: Callable[[Path], None]) -> bool:
    """The one home write: commit any hand edit first, apply, then one commit and
    a best-effort push. Refuses outside the lock. Returns whether the push landed."""
    if home.resolve() not in _HELD:
        raise RuntimeError("home write outside the lock")
    if _git(home, "status", "--porcelain"):
        _git(home, "add", "-A")
        _git(home, "commit", "-q", "-m", "focus: hand edit")
    apply(home)
    _git(home, "add", "-A")
    if _git(home, "diff", "--cached", "--quiet", check=False).returncode != 0:
        _git(home, "commit", "-q", "-m", message)
    return push(home)


def init_command() -> int:
    if owner_only("lime dash init"):
        return 2
    r = _gh("auth", "status")
    if r.returncode != 0:
        print(
            f"dashboard: gh is not authenticated ({_first_line(r.stderr)}) — run gh auth login",
            file=sys.stderr,
        )
        return 2
    r = _gh("api", "user", "--jq", ".login")
    account = r.stdout.strip()
    if r.returncode != 0 or not account:
        print(
            f"dashboard: could not read the active gh account ({_gh_reason(r)}) — run gh auth login",
            file=sys.stderr,
        )
        return 2
    print(f"dashboard: gh account {account}")

    h = home_dir()
    if _has_commit(h) and _origin_url(h):
        print(f"dashboard: already set up (origin = {_origin_url(h)})")
        return 0

    with locked(h):
        if not _has_commit(h):
            _git(h, "init", "-q", "-b", "main")
            now = _now()
            for name, text in (
                (".gitignore", GITIGNORE_TEXT),
                ("focus.md", FOCUS_HEADER),
                ("last-visit", _visit_text(now, now)),
            ):
                path = h / name
                if not path.exists():
                    path.write_text(text)
            _git(h, "add", "-A")
            _git(h, "commit", "-q", "-m", "dashboard init")
        if not _origin_url(h):
            r = _gh(
                "repo", "create", f"{account}/{REMOTE_NAME}",
                "--private", "--source", str(h), "--remote", "origin",
            )
            if r.returncode != 0:
                print(
                    f"dashboard: gh repo create {account}/{REMOTE_NAME} failed"
                    f" ({_gh_reason(r)}) — fix it and re-run lime dash init",
                    file=sys.stderr,
                )
                return 1
        push(h)

    print(f"dashboard: home repo ready at {h} (origin = {_origin_url(h)})")
    return 0


def visit_command() -> int:
    """Record an open of home: `previous ← current` after a 30-min gap, then
    print both stamps. Inside the gap nothing is written (no hand-edit commit,
    no push); a failed push still returns 0."""
    if owner_only("lime dash visit"):
        return 2
    h = home_dir()
    if not _has_commit(h):
        print(f"dashboard: no home repo at {h} — run lime dash init", file=sys.stderr)
        return 1
    with locked(h):
        now = _now()
        stamps = _read_visit(h)
        if stamps is None:
            previous = current = now
            write(
                h, f"visit {_stamp(current)}",
                lambda p: (p / "last-visit").write_text(_visit_text(previous, current)),
            )
        elif now - stamps[1] > VISIT_GAP:
            previous, current = stamps[1], now
            write(
                h, f"visit {_stamp(current)}",
                lambda p: (p / "last-visit").write_text(_visit_text(previous, current)),
            )
        else:
            previous, current = stamps
    print(_visit_text(previous, current), end="")
    return 0
