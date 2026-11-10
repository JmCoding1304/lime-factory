"""The batch's own record (ticket 254): one beating `processes` row of kind `batch`
(`run_id` = batch_id) plus `batch_children`, which mirrors `_run_batch`'s memory.

Nothing reads it to decide a launch — the in-memory `pending` queue stays the sole
authority for scheduling. This module is the only writer of `batch_children`: the
queue is mirrored for observability, never consulted. A failed write is logged and
retried on the next flush, never raised; a launch, an outcome, the exit code and the
report never depend on the ledger. A retry batch's row carries `retry_of`.

A retry reads beyond the queue: `child_run`/`child_fate` look a started child's Run
up and decide its fate, so the retry can take over a `started` child its dead batch
left behind (ticket 256). Those are pure reads too.
"""

import contextlib
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import tracker, tracer
from .runner import KilledBySignal
from .utils import new_id

logger = logging.getLogger(__name__)

KIND = "batch"
QUEUED = "queued"
STARTED = "started"
DROPPED = "dropped"
SLOT = "slot"
NO_TICKET_FILE = "no ticket file"
OVERLAP_CHECK_FAILED = "overlap check failed"

# A started child's fate when a retry reads its Run (ticket 256).
LIVE = "live"
PASSED = "passed"
KILLED = "killed"
FAILED = "failed"
DEPLOYED = "deployed"

_LEADING_DIGITS = re.compile(r"\d+")


def _now() -> str:
    """The one clock this module uses; a module function so tests can patch it."""
    return datetime.now(UTC).isoformat()


def blocked_reason(deps: list[str]) -> str:
    """`blocked 99` (or the file's name when it carries no leading digits)."""
    names = []
    for dep in deps:
        name = Path(dep).name
        match = _LEADING_DIGITS.match(name)
        names.append(match.group(0) if match else name)
    return "blocked " + ", ".join(names)


def overlap_reason(holders: Iterable[str]) -> str:
    """`overlap <holder>, <holder>` for the branches a candidate clashes with."""
    return "overlap " + ", ".join(holders)


def child_run(
    con: sqlite3.Connection, batch_id: str, ticket: str
) -> sqlite3.Row | None:
    """The newest Run this batch started for `ticket` (ticket 256).

    Matches a Run's stored `ticket` path the same way `cli._resolve_deploy_run`
    does — `tracker.stem_matches` on `Path(...).stem`: the stem, a stem prefix, or
    a bare number whatever its zero padding — but scoped to `kind='run' AND
    batch_id=?`, newest `started_at` first. No match, or no `runs` rows at all,
    reads None. Pure read.
    """
    rows = con.execute(
        "SELECT * FROM runs WHERE kind='run' AND batch_id=? ORDER BY started_at DESC",
        (batch_id,),
    ).fetchall()
    for row in rows:
        stem = Path(row["ticket"]).stem if row["ticket"] else None
        if stem and tracker.stem_matches(stem, ticket):
            return row
    return None


def run_dead(con: sqlite3.Connection, run_id: str, now: datetime) -> bool:
    """Whether a Run has ended without closing its row (ticket 256).

    True only when `tracer.liveness` reads `dead` and no open `kind='run'`
    `processes` row still holds a live pid — a beat gone stale over Mac sleep
    never reads dead while the pid lives. Pure read.
    """
    if tracer.liveness(con, run_id, now) != "dead":
        return False
    for row in con.execute(
        "SELECT pid FROM processes WHERE run_id=? AND kind='run' AND ended_at IS NULL",
        (run_id,),
    ):
        if tracer.pid_alive(row["pid"]):
            return False
    return True


def killed_by_signal(con: sqlite3.Connection, run_id: str) -> bool:
    """Whether a Run's death was a signal, not a failure of its own (ticket 256).

    True when the newest `crash_detected` event carries a truthy `signal` and
    its `event_id` is greater than the Run's newest `phase_start` event_id (no
    `phase_start` at all → True). A kill followed by a resume that later failed
    on its own left a newer `phase_start`, so it reads False. Pure read.
    """
    crash = con.execute(
        "SELECT event_id, payload_json FROM events"
        " WHERE run_id=? AND type='crash_detected' ORDER BY event_id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if crash is None:
        return False
    payload = json.loads(crash["payload_json"]) if crash["payload_json"] else {}
    if not payload.get("signal"):
        return False
    last_phase = con.execute(
        "SELECT max(event_id) AS event_id FROM events"
        " WHERE run_id=? AND type='phase_start'",
        (run_id,),
    ).fetchone()["event_id"]
    return last_phase is None or crash["event_id"] > last_phase


def child_fate(con: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> str:
    """A started child Run's fate, for a retry to take over (ticket 256).

    `running` reads `KILLED` when `run_dead`, else `LIVE`; `success` reads
    `PASSED`; `deployed` reads `DEPLOYED`; a `fail` whose `killed_by_signal`
    reads `KILLED`; everything else — a `fail` without a signal,
    `budget_exhausted`, `discarded`, an unknown status — reads `FAILED`.
    Pure read.
    """
    status = row["status"]
    if status == "running":
        return KILLED if run_dead(con, row["run_id"], now) else LIVE
    if status == "success":
        return PASSED
    if status == "deployed":
        return DEPLOYED
    if status == "fail" and killed_by_signal(con, row["run_id"]):
        return KILLED
    return FAILED


def watch_log(runs_dir: Path, batch_id: str) -> Path:
    """The watchdog's log for one batch."""
    return Path(runs_dir) / f"watchdog-{batch_id}.log"


def _log(path: Path, line: str) -> None:
    """Best-effort one-line append; a missing parent is created, an OSError swallowed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as log:
            log.write(f"{_now()} {line}\n")
    except OSError:
        pass


def unfinished_tickets(con: sqlite3.Connection, batch_id: str) -> list[str]:
    """A batch's `queued` and `started` children, in position order (ticket 256).

    A retry reads a `started` row too: its Run was launched in the dead batch and
    now needs taking over, not a second birth.
    """
    return [
        row["ticket"]
        for row in con.execute(
            "SELECT ticket FROM batch_children"
            " WHERE batch_id=? AND state IN (?, ?) ORDER BY position",
            (batch_id, QUEUED, STARTED),
        )
    ]


def retry_command(
    batch_id: str, tickets: list[str], *, deploy: bool, profile: str | None
) -> list[str]:
    """The retry batch's `lime run` argv, mirroring the dead batch's flags."""
    return [
        sys.executable, "-m", "lime", "run", *tickets, "--retry-of", batch_id,
        *(["--deploy"] if deploy else []),
        *(["--profile", profile] if profile else []),
    ]


def _spawn_retry(cmd: list[str], cwd: Path, log) -> subprocess.Popen:
    """The seam tests patch; a real detached retry batch, log to the watchdog's log."""
    return subprocess.Popen(
        cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT
    )


def _unclaimed_reason(con: sqlite3.Connection, batch_id: str) -> str:
    """Why `claim_retry` refused: a closed row, a claimed row, a retry's row or none."""
    row = con.execute(
        "SELECT ended_at, retried_at, retry_of FROM processes"
        " WHERE run_id=? AND kind='batch'",
        (batch_id,),
    ).fetchone()
    if row is None:
        return "no batch row"
    if row["ended_at"] is not None:
        return "batch row closed"
    if row["retry_of"] is not None:
        return "a retry is never retried"
    return "already claimed"


def retry_dead(
    project_root: Path, batch_id: str, *, deploy: bool = False, profile: str | None = None
) -> bool:
    """Claim a dead batch and launch one retry. True only for the caller that claimed.

    The retry takes its dead batch's `queued` and `started` children (ticket 256):
    the started ones are already Runs somewhere in their lifecycle, so the retry's
    `_run_batch` takes each over by its Run's fate rather than birthing a second one.
    """
    runs_dir = (Path(project_root) / ".lime" / "runs").resolve()
    log_path = watch_log(runs_dir, batch_id)
    tr = tracer.Tracer(runs_dir)
    try:
        if not tr.claim_retry(batch_id):
            _log(log_path, f"{batch_id}: not retried — {_unclaimed_reason(tr.con, batch_id)}")
            return False
        tickets = unfinished_tickets(tr.con, batch_id)
    finally:
        tr.con.close()
    if not tickets:
        _log(log_path, f"{batch_id}: claimed — nothing left to retry")
        return True
    cmd = retry_command(batch_id, tickets, deploy=deploy, profile=profile)
    _log(log_path, f"{batch_id}: claimed — launching retry: {' '.join(cmd[2:])}")
    try:
        with log_path.open("a") as log:
            proc = _spawn_retry(cmd, Path(project_root), log)
    except OSError as exc:
        _log(log_path, f"{batch_id}: retry failed to launch: {exc}")
        return True
    code = proc.wait()
    _log(log_path, f"{batch_id}: retry exited {code}")
    return True


def watch(
    project_root: Path,
    batch_id: str,
    *,
    deploy: bool = False,
    profile: str | None = None,
    stream=None,
) -> int:
    """Block on the batch's pipe until it closes, then retry the batch once if it died."""
    runs_dir = (Path(project_root) / ".lime" / "runs").resolve()
    log_path = watch_log(runs_dir, batch_id)
    source = stream if stream is not None else sys.stdin.buffer
    try:
        source.read()
    except OSError as exc:
        _log(log_path, f"{batch_id}: watchdog lost its pipe: {exc} — not retried")
        return 1
    try:
        retry_dead(Path(project_root), batch_id, deploy=deploy, profile=profile)
    except Exception as exc:  # noqa: BLE001 — a watchdog failure never crashes the batch
        _log(log_path, f"{batch_id}: watchdog error: {exc}")
        return 1
    return 0


@dataclass
class Child:
    ticket: str
    state: str = QUEUED
    reason: str | None = None
    updated_at: str = ""


class Batch:
    """One batch's ledger mirror: an owning `processes` row plus one child per arg."""

    def __init__(self, project_root: Path, tickets: list[str], retry_of: str | None = None):
        self.project_root = Path(project_root)
        self.batch_id = new_id(8)
        self.retry_of = retry_of
        self.runs_dir = (self.project_root / ".lime" / "runs").resolve()
        self.pid = os.getpid()
        self.children = [
            Child(
                ticket=(path.stem if (path := tracker.find(self.project_root, arg)) else arg),
                updated_at=_now(),
            )
            for arg in tickets
        ]
        self._dirty: set[int] = set()
        self._tr: tracer.Tracer | None = None
        self._process_id: int | None = None
        self._closed = False
        self._saved_handlers: dict[int, object] = {}
        self._watch_fd: int | None = None
        self._watchdog: subprocess.Popen | None = None

    @classmethod
    def open(
        cls, project_root: Path, tickets: list[str], retry_of: str | None = None
    ) -> "Batch":
        """Construct, install the signal close, and write the opening row.

        Never raises on a ledger failure: an unopened mirror is retried on the next
        flush, and the batch itself carries on regardless.
        """
        batch = cls(project_root, tickets, retry_of)
        batch._install_signal_close()
        batch.flush()
        return batch

    # --- in-memory transitions (never raise, never touch the ledger) ---

    def _transition(self, position: int, state: str, reason: str | None) -> None:
        child = self.children[position]
        if child.state != QUEUED:
            return
        child.state = state
        child.reason = reason
        child.updated_at = _now()
        self._dirty.add(position)

    def start(self, position: int) -> None:
        """A queued child is now in flight."""
        self._transition(position, STARTED, None)

    def drop(self, position: int, reason: str) -> None:
        """A queued child will never run this batch."""
        self._transition(position, DROPPED, reason)

    def give_up(self, position: int) -> None:
        """Drop a still-queued child at the stall branch, keeping its last hold reason."""
        reason = self.children[position].reason
        self.drop(position, f"gave up: {reason}" if reason else "gave up")

    def hold(self, position: int, reason: str) -> None:
        """A queued child stays put; a no-op when its reason is unchanged."""
        child = self.children[position]
        if child.state != QUEUED or child.reason == reason:
            return
        child.reason = reason
        child.updated_at = _now()
        self._dirty.add(position)

    def wait_for_slot(self, positions: Iterable[int]) -> None:
        """Queued children with no reason yet are waiting on the port cap."""
        for position in positions:
            child = self.children[position]
            if child.state == QUEUED and child.reason is None:
                self.hold(position, SLOT)

    # --- ledger mirror ---

    def _write(self) -> None:
        if self._tr is None:
            self._tr = tracer.Tracer(self.runs_dir)
        tr = self._tr
        con = tr.con
        opening = self._process_id is None
        process_id: int | None = None
        try:
            if opening:
                process_id = tr.start_process(
                    self.batch_id,
                    kind=KIND,
                    name=Path(sys.argv[0]).stem,
                    pid=self.pid,
                    command=" ".join([Path(sys.argv[0]).name, *sys.argv[1:]]),
                    retry_of=self.retry_of,
                    commit=False,
                )
                con.executemany(
                    "INSERT INTO batch_children"
                    " (batch_id, position, ticket, state, reason, updated_at)"
                    " VALUES (?,?,?,?,?,?)",
                    [
                        (self.batch_id, pos, child.ticket, child.state, child.reason, child.updated_at)
                        for pos, child in enumerate(self.children)
                    ],
                )
            else:
                con.executemany(
                    "UPDATE batch_children SET state=?, reason=?, updated_at=?"
                    " WHERE batch_id=? AND position=?",
                    [
                        (
                            self.children[pos].state,
                            self.children[pos].reason,
                            self.children[pos].updated_at,
                            self.batch_id,
                            pos,
                        )
                        for pos in sorted(self._dirty)
                    ],
                )
            con.commit()
        except BaseException:
            # Atomic: a half-written opening or update rolls back as one, and the
            # caller logs and retries on the next pass rather than raising.
            with contextlib.suppress(sqlite3.Error):
                con.rollback()
            raise
        if opening:
            self._process_id = process_id
            tracer.start_heartbeat(self.runs_dir / "lime.db", process_id)
        self._dirty.clear()

    def flush(self) -> bool:
        """Mirror the current queue. `True` when nothing needed writing or it wrote."""
        if self._closed:
            return True
        if self._process_id is not None and not self._dirty:
            return True
        try:
            self._write()
        except Exception as exc:  # noqa: BLE001 — the ledger never blocks the batch
            logger.warning(
                "lime: batch %s mirror write failed: %s — retrying on the next pass",
                self.batch_id, exc,
            )
            return False
        return True

    def _end_row(self) -> None:
        if self._tr is None:
            return
        try:
            self._tr.end_process(self.batch_id, self.pid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("lime: batch %s end write failed: %s", self.batch_id, exc)

    def close(self) -> None:
        """Flush, close the beating row, restore the signals and the connection. Idempotent."""
        if self._closed:
            return
        self.flush()
        self._end_row()
        if self._watch_fd is not None:
            # Close the pipe: the watchdog's stdin read then returns and it finds
            # the row closed, so it exits without retrying.
            with contextlib.suppress(OSError):
                os.close(self._watch_fd)
            self._watch_fd = None
        self._closed = True
        for sig, handler in self._saved_handlers.items():
            if handler is None:
                continue
            with contextlib.suppress(ValueError, TypeError):
                signal.signal(sig, handler)
        if self._tr is not None:
            with contextlib.suppress(Exception):
                self._tr.con.close()

    # --- watchdog (ticket 255) ---

    def start_watchdog(
        self, *, deploy: bool = False, profile: str | None = None
    ) -> subprocess.Popen | None:
        """Spawn the watchdog that retries this batch once if it dies.

        It runs in its own session, so Ctrl-C and a closed terminal never reach it;
        only this process holds the write end of the pipe (os.pipe fds are
        non-inheritable and Popen closes fds, so child Runs never hold it), so any
        end of this process — exit, crash, SIGKILL — closes the pipe. A retry starts
        none, so a retry is never retried.
        """
        if self.retry_of is not None or self._watchdog is not None or self._closed:
            return None
        read_fd, write_fd = os.pipe()
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        try:
            with watch_log(self.runs_dir, self.batch_id).open("a") as log:
                proc = subprocess.Popen(
                    [
                        sys.executable, "-m", "lime", "batch", "watch", self.batch_id,
                        *(["--deploy"] if deploy else []),
                        *(["--profile", profile] if profile else []),
                    ],
                    cwd=self.project_root,
                    stdin=read_fd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            os.close(write_fd)
            logger.warning(
                "lime: batch %s watchdog did not start: %s — no retry if this batch dies",
                self.batch_id, exc,
            )
            return None
        finally:
            os.close(read_fd)
        self._watch_fd = write_fd
        self._watchdog = proc
        return proc

    # --- signal close ---

    def _install_signal_close(self) -> None:
        """SIGTERM/SIGINT only; never SIGHUP. A non-main thread gets no handler."""
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._saved_handlers[sig] = signal.signal(sig, self._on_signal)
            except ValueError:
                pass

    def _on_signal(self, signum, _frame):
        self._end_row()
        raise KilledBySignal(signum)
