"""Trace writer for a Run: the nine-table SQLite mirror + per-Run files.

Contract the schema can't show: a `turn` event's `name` column holds the
session_id that produced it — `lime cost` joins on it, so every adapter
writing turns must set it.

A summarizer Session belongs to no Run: its `sessions` row and `turn` events
carry `NO_RUN`, so `lime cost` counts it and nothing lists it as a Run.

A `log` event named `phase_invalidated` marks its `phase_id` as no longer
replayable; that is how a Run whose step exhausted its build attempts
stops `--resume` replaying a stale plan.
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

from .data_types import PhaseStage
from .mask import mask_tree

logger = logging.getLogger(__name__)

# The beat loop's own sleep, bound once at import so tests that monkeypatch the
# shared `time.sleep` module attribute (e.g. to observe an agent's backoff) never
# turn the heartbeat thread into a busy loop.
_sleep = time.sleep

INVALIDATED = "phase_invalidated"
REREVIEW_DECISION = "rereview_decision"
_REVIEW_PHASE = re.compile(r"review(_retry\d+)?")
_VERIFY_PHASE = re.compile(r"verify_\d+(_retry\d+)?")

STAGES = get_args(PhaseStage)
OTHER_STAGE = "Other"

# The owner's table (2026-11-06, ticket 251). It is used only to fill rows written
# before Phases declared a stage; live Phases declare their own.
_STAGE_TABLE = (
    ("Prepare", re.compile(r"engineer|setup|scout|plan|research")),
    ("Build", re.compile(r"build_\d+(_retry\d+)?|verify_\d+(_retry\d+)?|design|design_continue")),
    ("Review", re.compile(r"review(_retry\d+)?|revise_\d+|commit_code|changes|design_lock")),
    ("Ship", re.compile(r"document|commit_docs|push_pr|deploy")),
)


def legacy_stage(name: str) -> str:
    """The stage the owner's table assigns `name`, or `OTHER_STAGE` when it knows none."""
    for stage, pattern in _STAGE_TABLE:
        if pattern.fullmatch(name):
            return stage
    return OTHER_STAGE


# A refusal raised before any Run row exists carries this run_id. It names no
# Run, has no Run folder, and its db row is the only record. A summarizer
# Session also carries it: its `sessions` row and `turn` events are its whole
# record, so `lime cost` counts it and nothing lists it as a Run.
NO_RUN = "-"

EVENT_TYPES = frozenset(
    {
        # the original ten
        "phase_start",
        "phase_end",
        "agent_start",
        "agent_end",
        "tool_call",
        "handoff",
        "gate_pass",
        "gate_fail",
        "log",
        "error",
        # budget & continuation (spec §9)
        "budget_warn",
        "budget_stop",
        "budget_kill",
        "continuation",
        # one per model answer — the live spend feed
        "turn",
        # ticket 63: a signal death, visible in the trace
        "crash_detected",
        # ticket 82: time ceiling nudge and kill
        "timeout_nudge",
        "timeout_kill",
        # ticket 173: identical tool calls in a row tripped the repeat stop
        "repeat_stop",
        # ticket 216: a launch or deploy lime refused or held
        "refusal",
        # ticket 250: one per tool call — ok, duration, output tail
        "tool_result",
    }
)

RUN_TRANSITIONS = {
    "running": {"success", "fail", "budget_exhausted", "discarded"},
    "success": {"deployed", "discarded"},
    # `lime run --resume` restarts the failed Phase (§3; budget after re-scope, §9)
    "fail": {"running", "discarded"},
    "budget_exhausted": {"running", "discarded"},
    "deployed": set(),
    "discarded": set(),
}

# A status no code path sets any more, kept so a row written before its removal still
# loads and can be discarded (ticket 194: needs_rescope Runs preserved before ticket 193).
LEGACY_RUN_TRANSITIONS = {"needs_rescope": {"discarded"}}

PHASE_TRANSITIONS = {
    "queued": {"running"},
    "running": {"success", "fail"},
    "fail": {"running"},  # retry: attempt increments
    "success": set(),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    workflow     TEXT NOT NULL,
    request      TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    engineer     TEXT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost   REAL NOT NULL DEFAULT 0,
    archived     INTEGER NOT NULL DEFAULT 0,
    ticket       TEXT,
    branch       TEXT,
    worktree     TEXT,
    port         INTEGER,
    db_name      TEXT,
    profile      TEXT,
    base_sha     TEXT,
    pr_number    INTEGER,
    pr_url       TEXT,
    batch_id     TEXT,
    kind         TEXT NOT NULL DEFAULT 'run'
);
CREATE TABLE IF NOT EXISTS phases (
    phase_id    TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    owner       TEXT,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',
    attempt     INTEGER NOT NULL DEFAULT 1,
    retries     INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    started_at  TEXT,
    ended_at    TEXT,
    stage       TEXT
);
CREATE TABLE IF NOT EXISTS events (
    event_id     INTEGER PRIMARY KEY,
    run_id       TEXT NOT NULL,
    phase_id     TEXT,
    parent_id    INTEGER,
    type         TEXT NOT NULL,
    name         TEXT,
    payload_json TEXT,
    tokens       INTEGER,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    seq          INTEGER
);
CREATE TABLE IF NOT EXISTS envelopes (
    envelope_id  INTEGER PRIMARY KEY,
    run_id       TEXT NOT NULL,
    phase_id     TEXT,
    agent        TEXT,
    output_type  TEXT,
    payload_json TEXT,
    valid        INTEGER,
    attempt      INTEGER,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gate_results (
    id              INTEGER PRIMARY KEY,
    run_id          TEXT NOT NULL,
    phase_id        TEXT,
    attempt         INTEGER,
    gate            TEXT NOT NULL,
    passed          INTEGER NOT NULL,
    violations_json TEXT,
    checks_json     TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processes (
    id         INTEGER PRIMARY KEY,
    run_id     TEXT NOT NULL,
    kind       TEXT,
    name       TEXT,
    pid          INTEGER,
    command      TEXT,
    started_at   TEXT,
    ended_at     TEXT,
    heartbeat_at TEXT,
    retried_at   TEXT,
    retry_of     TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    phase_id       TEXT NOT NULL,
    role           TEXT NOT NULL,
    continuation   INTEGER NOT NULL DEFAULT 0,
    runtime        TEXT NOT NULL,
    model          TEXT NOT NULL,
    color          TEXT,
    context_tokens INTEGER,
    context_window INTEGER,
    created_at     TEXT NOT NULL,
    last_used_at   TEXT,
    budget_warn    INTEGER,
    budget_stop    INTEGER
);
-- Written only by `lime/batch.py`: a mirror of a batch's queue that is never read to decide a launch.
CREATE TABLE IF NOT EXISTS batch_children (
    batch_id   TEXT NOT NULL,
    position   INTEGER NOT NULL,
    ticket     TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'queued',
    reason     TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, position)
);
CREATE TABLE IF NOT EXISTS summaries (
    item_key   TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    state      TEXT NOT NULL,
    summary    TEXT,
    error      TEXT,
    owner_pid  INTEGER,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def pid_alive(pid: int | None) -> bool:
    """Best-effort liveness probe — a crashed Run's port must count as free."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    except OSError:
        return False
    return True


HEARTBEAT_S = 10.0


def _beat(con: sqlite3.Connection, process_id: int) -> int:
    """Stamp one open process row's `heartbeat_at`; the only writer of that column.

    Returns the UPDATE's rowcount, so a 0 means the row is closed or gone.
    """
    cur = con.execute(
        "UPDATE processes SET heartbeat_at=? WHERE id=? AND ended_at IS NULL",
        (_now(), process_id),
    )
    con.commit()
    return cur.rowcount


def _beat_until_closed(db_path: Path, process_id: int, every: float) -> None:
    """Thread body: beat `process_id`'s row every `every` seconds until it closes.

    Connection establishment and the beat write both live inside the retry
    handler: a transient failure of either is logged and retried on the next
    beat, never raised. The first attempt is immediate. When the UPDATE touches 0
    rows the row is closed or gone, so the thread returns; a failure whose ledger
    file has vanished (a bare scout's deleted temp dir) does the same.
    """
    con: sqlite3.Connection | None = None
    try:
        while True:
            try:
                if con is None:
                    con = connect(db_path)
                if _beat(con, process_id) == 0:
                    return
            except Exception as exc:  # noqa: BLE001 — one failed beat must never kill the run
                logger.warning(
                    "lime: heartbeat for process row %s failed: %s — retrying in %ss",
                    process_id, exc, every,
                )
                if not db_path.exists():
                    return
            _sleep(every)
    finally:
        if con is not None:
            con.close()


def start_heartbeat(
    db_path: Path | str, process_id: int, *, interval: float | None = None
) -> threading.Thread:
    """Start the daemon thread that beats `process_id`'s row until it closes.

    Alive is not busy: the beat only proves the owning process still exists.
    SIGKILL or a crash stops the beat with the process, and `--resume` gets a new
    row and a new thread while the old row stops beating.
    """
    thread = threading.Thread(
        target=_beat_until_closed,
        args=(Path(db_path), process_id, HEARTBEAT_S if interval is None else interval),
        name=f"lime-heartbeat-{process_id}",
        daemon=True,
    )
    thread.start()
    return thread


LIVENESS_S = 45.0  # three missed 10 s beats plus slack


def _stamp(text: str | None) -> datetime | None:
    """Parse an ISO stamp; a naive one reads as UTC, and None/garbage reads as None."""
    if text is None:
        return None
    try:
        stamp = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp


def _processes_has_heartbeat(con: sqlite3.Connection) -> bool:
    """Whether `processes` carries the `heartbeat_at` column (`row[1]` is the name)."""
    return any(row[1] == "heartbeat_at" for row in con.execute("PRAGMA table_info(processes)"))


def liveness(
    conn: sqlite3.Connection, run_id: str, now: datetime, kind: str = "run"
) -> str:
    """One rule, two readers: `lime status` and the dashboard feed (ticket 258).

    Returns `"live" | "starting" | "dead"` for `run_id`'s rows of `kind`. A row of
    `kind` with a beat younger than `LIVENESS_S`, or with a NULL/unparseable beat
    whose pid is alive, is `live`; with no row of that kind at all, a `runs.started_at`
    younger than `LIVENESS_S` is `starting`; everything else, including no `runs` row,
    is `dead`. It only reads, so `lime status` can hand it a read-only connection;
    the shared corpus lives in `tests/fixtures/liveness.json`. The launch guards
    (`_held_ports`, `live_pid`, `_run_is_live`) keep their own pid check.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    # A read-only open of an un-migrated db cannot ALTER the column in, so read it
    # only when it exists; otherwise every beat reads as NULL and the pid check decides.
    beat = "heartbeat_at" if _processes_has_heartbeat(conn) else "NULL AS heartbeat_at"
    rows = conn.execute(
        f"SELECT pid, ended_at, {beat} FROM processes WHERE run_id=? AND kind=?",
        (run_id, kind),
    ).fetchall()
    for row in rows:
        pid, ended_at, heartbeat_at = row[0], row[1], row[2]
        if ended_at:
            continue
        beat_at = _stamp(heartbeat_at)
        if beat_at is not None:
            if (now - beat_at).total_seconds() < LIVENESS_S:
                return "live"
        elif pid_alive(pid):
            return "live"
    if rows:
        return "dead"
    try:
        start_row = conn.execute(
            "SELECT started_at FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return "dead"
    if start_row is not None:
        started_at = _stamp(start_row[0])
        if started_at is not None and (now - started_at).total_seconds() < LIVENESS_S:
            return "starting"
    return "dead"


class PortsExhausted(RuntimeError):
    """None of `runs.ports` is free — every one is held by a live Run's pid (spec §7)."""

    def __init__(self, held: dict[int, str]):
        self.held = held
        named = ", ".join(f"{run_id} (port {port})" for port, run_id in sorted(held.items()))
        super().__init__(f"no free port — live Runs: {named}" if named else "no free port")


# Columns added to `runs` after its first release. A read-write open ALTERs each
# absent one in; a read-only open cannot, so it shadows `runs` with a per-connection
# TEMP view that reads each absent one as NULL. The ledger file is never written by a
# reader.
_RUNS_ADDED_COLUMNS: dict[str, str] = {
    "base_sha": "TEXT",
    "pr_number": "INTEGER",
    "pr_url": "TEXT",
    "batch_id": "TEXT",
}

# columns added to events/phases/sessions after first release; a writer open ALTERs
# each absent one in.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "events": {"seq": "INTEGER"},
    "sessions": {"budget_warn": "INTEGER", "budget_stop": "INTEGER"},
    "phases": {"stage": "TEXT"},
    "processes": {"heartbeat_at": "TEXT", "retried_at": "TEXT", "retry_of": "TEXT"},
}

# Ledger indexes, each mapping index name → `table(columns)`. Created only by a
# writer open (see `_migrate`): the dashboard and a read-only `connect` cannot. No
# index names a column from `_RUNS_ADDED_COLUMNS`, which the ALTERs add first anyway.
_INDEXES: dict[str, str] = {
    # Per-Run event reads, and `last_event_at` = max per Run in `lime runs` and the
    # dashboard.
    "ix_events_run_started": "events(run_id, started_at)",
    # The spend window (`type='turn' AND started_at >= ?`) and per-type counts.
    "ix_events_type_started": "events(type, started_at)",
    "ix_phases_run": "phases(run_id, seq)",
    "ix_sessions_run": "sessions(run_id)",
    # The newest-first list.
    "ix_runs_started": "runs(started_at)",
    "ix_envelopes_run": "envelopes(run_id)",
    "ix_gate_results_run": "gate_results(run_id)",
}


def _runs_columns(con: sqlite3.Connection) -> list[str]:
    """The live `runs` column names in table order; `[]` when there is no table.

    `row[1]` is the name, so this does not depend on the connection's row_factory.
    """
    return [row[1] for row in con.execute("PRAGMA table_info(runs)")]


def runs_lacks_added_columns(con: sqlite3.Connection) -> bool:
    """True when `main.runs` predates `base_sha` (ticket 144) — a legacy ledger. A
    read-only connection shadows such a table with a TEMP view, so this inspects
    `main.runs` directly, never the shadowing view. A ledger with no `runs` table
    reports False. Later added columns (216's PR pair) do not make a ledger legacy.
    """
    present = {row[1] for row in con.execute("PRAGMA main.table_info(runs)")}
    return bool(present) and "base_sha" not in present


def connect(db_path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Every connection gets the same pragmas: WAL, NORMAL sync, 5 s busy wait.

    A read-only connection also sees the current `runs` shape."""
    if readonly:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db_path)
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    if readonly:
        present = _runs_columns(con)
        missing = [name for name in _RUNS_ADDED_COLUMNS if name not in present]
        if present and missing:
            # List the present columns explicitly (never `*`): a concurrent read-write
            # migration mid-connection cannot then produce duplicate column names.
            listed = ", ".join(f'"{c}"' for c in present)
            nulls = ", ".join(f"NULL AS {name}" for name in missing)
            con.execute(f"CREATE TEMP VIEW runs AS SELECT {listed}, {nulls} FROM main.runs")
    return con


class Tracer:
    """Writer for one project's `.lime/runs/`: db rows and the per-Run files."""

    def __init__(self, runs_dir: Path | str):
        self.runs_dir = Path(runs_dir)
        self.con = connect(self.runs_dir / "lime.db")
        self.con.executescript(_SCHEMA)
        self._migrate()
        self.con.commit()

    def _migrate(self) -> None:
        """Narrow, idempotent column and index migrations for databases created by
        older builds.

        Inspect the live table and ALTER only when the column is absent, so
        reopening an up-to-date db (or a from-scratch one) is a no-op. `runs`
        columns are still never backfilled — a legacy Run's `base_sha` stays NULL,
        which means legacy behavior, not an inferred base — but `events.seq` NULL
        rows are filled (`_fill_event_seq`), and `phases.stage` NULL rows are filled
        from the owner's table, unknown names getting `Other` (`_fill_phase_stage`).
        Indexes are declared with
        `IF NOT EXISTS` and are created only by a writer open: the dashboard and a
        read-only `connect` cannot, so an interrupted migration keeps what it built
        and the next writer open adds the rest.
        """
        present = set(_runs_columns(self.con))
        for name, type_ in _RUNS_ADDED_COLUMNS.items():
            if name not in present:
                self._add_column("runs", name, type_)
        for table, columns in _ADDED_COLUMNS.items():
            existing = {row[1] for row in self.con.execute(f"PRAGMA table_info({table})")}
            for name, type_ in columns.items():
                if name not in existing:
                    self._add_column(table, name, type_)
        for name, spec in _INDEXES.items():
            self.con.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {spec}")
        self.con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_events_run_seq ON events(run_id, seq)"
        )
        self._fill_event_seq()
        self._fill_phase_stage()

    def _add_column(self, table: str, name: str, type_: str) -> None:
        """`ALTER TABLE … ADD COLUMN`, tolerating the one case where another writer
        won the race and added it first; anything else re-raises."""
        try:
            self.con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {type_}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise

    def _fill_event_seq(self) -> None:
        """Number every NULL `events.seq` after its Run's highest seq, in `event_id`
        order. No-op (and no write lock) when there is nothing to fill; numbered rows
        are never renumbered, so a second migration changes nothing."""
        if self.con.execute("SELECT 1 FROM events WHERE seq IS NULL LIMIT 1").fetchone() is None:
            return
        self.con.execute(
            "UPDATE events SET seq = fill.seq FROM ("
            "  SELECT e.event_id,"
            "         COALESCE((SELECT MAX(m.seq) FROM events m WHERE m.run_id = e.run_id), 0)"
            "         + ROW_NUMBER() OVER (PARTITION BY e.run_id ORDER BY e.event_id) AS seq"
            "  FROM events e WHERE e.seq IS NULL) AS fill"
            " WHERE events.event_id = fill.event_id"
        )

    def _fill_phase_stage(self) -> None:
        """Give every NULL `phases.stage` its name's stage from the owner's table.

        Only names written before Phases declared a stage are touched; a live Phase's
        declared stage is never overwritten. No-op (and no write lock) when there is
        nothing to fill, so a second migration changes nothing."""
        names = [
            r[0]
            for r in self.con.execute("SELECT DISTINCT name FROM phases WHERE stage IS NULL")
        ]
        for name in names:
            self.con.execute(
                "UPDATE phases SET stage=? WHERE name=? AND stage IS NULL",
                (legacy_stage(name), name),
            )

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    @staticmethod
    def _session_name(seq: int, role: str, ordinal: int) -> str:
        suffix = f".{ordinal}" if ordinal else ""
        return f"{seq:02d}-{role}{suffix}"

    def session_dir(self, run_id: str, seq: int, role: str, continuation: int = 0, session_index: int | None = None) -> Path:
        ordinal = session_index if session_index is not None else continuation
        return self.run_dir(run_id) / self._session_name(seq, role, ordinal)

    def live_pid(self, run_id: str) -> int | None:
        """The pid of `run_id`'s current process, if it's registered, still open,
        and alive — None once that process row is closed or its pid is dead."""
        proc = self.con.execute(
            "SELECT pid FROM processes WHERE run_id=? AND kind='run' AND ended_at IS NULL"
            " ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return proc["pid"] if proc is not None and pid_alive(proc["pid"]) else None

    def _held_ports(self) -> dict[int, str]:
        """port -> run_id for every `running` Run whose slot might still be live.

        Conservative on purpose: a Run that just reserved its port has not
        registered its pid yet in the instant between `start_run` committing and
        `start_process` inserting — that sliver still counts as held, else a
        second concurrent `lime run` could land on the very same port before
        either registers. Only a Run whose process row is closed, or whose
        recorded pid is confirmed dead, frees its port back to the pool.
        """
        rows = self.con.execute(
            "SELECT run_id, port FROM runs WHERE status='running' AND port IS NOT NULL"
        ).fetchall()
        held: dict[int, str] = {}
        for row in rows:
            proc = self.con.execute(
                "SELECT pid, ended_at FROM processes WHERE run_id=? AND kind='run'"
                " ORDER BY id DESC LIMIT 1",
                (row["run_id"],),
            ).fetchone()
            if proc is None or (proc["ended_at"] is None and pid_alive(proc["pid"])):
                held[row["port"]] = row["run_id"]
        return held

    def port_holder(self, port: int, *, exclude_run_id: str | None = None) -> str | None:
        """The run_id currently holding `port` (spec §7), if any."""
        for held_port, run_id in self._held_ports().items():
            if held_port == port and run_id != exclude_run_id:
                return run_id
        return None

    def _insert_run_row(self, run_id: str, *, workflow, request, kind, engineer, ticket,
                         branch, worktree, port, db_name, profile, base_sha, batch_id) -> None:
        self.con.execute(
            "INSERT INTO runs (run_id, workflow, request, status, engineer,"
            " started_at, ticket, branch, worktree, port, db_name, profile, kind, base_sha, batch_id)"
            " VALUES (:run_id, :workflow, :request, 'running', :engineer,"
            " :started_at, :ticket, :branch, :worktree, :port, :db_name, :profile, :kind, :base_sha, :batch_id)",
            {
                "run_id": run_id, "workflow": workflow, "request": request, "kind": kind,
                "engineer": engineer, "ticket": ticket, "branch": branch, "worktree": worktree,
                "port": port, "db_name": db_name, "profile": profile,
                "base_sha": base_sha, "batch_id": batch_id, "started_at": _now(),
            },
        )

    def start_run(
        self,
        run_id: str,
        *,
        workflow: str,
        request: str,
        kind: str = "run",
        engineer: str | None = None,
        ticket: str | None = None,
        branch: str | None = None,
        worktree: str | None = None,
        port: int | None = None,
        ports: list[int] | None = None,
        db_name: str | None = None,
        profile: str | None = None,
        base_sha: str | None = None,
        batch_id: str | None = None,
    ) -> int | None:
        """One call writes both the row and the identity card (run.json).

        `ports`, not a bare `port`, is what makes allocation atomic: the free-port
        check and the row insert share one `BEGIN IMMEDIATE`, so two concurrent
        `lime run` processes can never land on the same one (spec §7/§8) — a
        crashed Run's pid-dead port counts as free. Returns the port actually
        stored, whichever way it got there.
        """
        if ports is not None:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                held = self._held_ports()
                free = [p for p in ports if p not in held]
                if not free:
                    raise PortsExhausted(held)
                port = free[0]
                self._insert_run_row(run_id, workflow=workflow, request=request, kind=kind,
                                      engineer=engineer, ticket=ticket, branch=branch,
                                      worktree=worktree, port=port, db_name=db_name,
                                      profile=profile, base_sha=base_sha, batch_id=batch_id)
            except BaseException:
                self.con.rollback()
                raise
            self.con.commit()
        else:
            self._insert_run_row(run_id, workflow=workflow, request=request, kind=kind,
                                  engineer=engineer, ticket=ticket, branch=branch,
                                  worktree=worktree, port=port, db_name=db_name,
                                  profile=profile, base_sha=base_sha, batch_id=batch_id)
            self.con.commit()

        card = {
            "run_id": run_id, "workflow": workflow, "request": request, "kind": kind,
            "engineer": engineer, "ticket": ticket, "branch": branch, "worktree": worktree,
            "port": port, "db_name": db_name, "profile": profile,
            "base_sha": base_sha, "started_at": _now(),
        }
        run = self.run_dir(run_id)
        (run / "context_handoff").mkdir(parents=True, exist_ok=True)
        (run / "run.json").write_text(json.dumps(card, indent=2) + "\n")
        (run / "events.jsonl").touch()
        return port

    def card(self, run_id: str) -> dict:
        """run.json as a dict; {} when the card is missing (a run that predates it)."""
        path = self.run_dir(run_id) / "run.json"
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def update_card(self, run_id: str, **fields) -> dict:
        """Merge fields into run.json \u2014 the resolved roster lands here at run/resume start (ticket 84)."""
        card = {**self.card(run_id), **fields}
        (self.run_dir(run_id) / "run.json").write_text(json.dumps(card, indent=2) + "\n")
        return card

    def run_exists(self, run_id: str) -> bool:
        row = self.con.execute(
            "SELECT 1 FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return row is not None

    def set_run_status(self, run_id: str, status: str) -> None:
        row = self.con.execute(
            "SELECT status FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown run {run_id!r}")
        old = row["status"]
        allowed = RUN_TRANSITIONS.get(old, LEGACY_RUN_TRANSITIONS.get(old, set()))
        if status not in allowed:
            raise ValueError(f"run {run_id}: {old} -> {status} not allowed")
        now = _now()
        self.con.execute(
            "UPDATE runs SET status=?,"
            # --resume reopens the run: the clock ticks again until it really finishes
            " ended_at = CASE WHEN ? = 'running' THEN NULL"
            # deployed/discarded are verdicts on an already-finished run — keep its finish time;
            # a running run that is discarded finishes now
            "   WHEN ? IN ('deployed', 'discarded') THEN coalesce(ended_at, ?)"
            # success / fail / budget_exhausted: this finish, never a stale one from before a resume
            "   ELSE ? END"
            " WHERE run_id=?",
            (status, status, status, now, now, run_id),
        )
        if status != "running":
            # a finished run leaves no process row open — a row that outlives
            # its pid would claim work is in flight that is already dead
            self.con.execute(
                "UPDATE processes SET ended_at=? WHERE run_id=? AND ended_at IS NULL",
                (now, run_id),
            )
        self.con.commit()

    def max_phase_seq(self, run_id: str) -> int:
        """A joined run continues the sequence instead of restarting it."""
        row = self.con.execute(
            "SELECT coalesce(max(seq), 0) AS seq FROM phases WHERE run_id=?", (run_id,)
        ).fetchone()
        return int(row["seq"])

    def replay_map(self, run_id: str) -> dict[str, dict]:
        """`name -> {phase_id, output_type, payload_json, seq}` for every Phase that already
        succeeded in this Run — what `lime run --resume` skips instead of re-running
        (spec §9: restart the failed Phase, not the chain). Latest success per name
        wins; the envelope fields are None when none was recorded.
        Invalidated successes (those with a `phase_invalidated` log event) are excluded."""
        out: dict[str, dict] = {}
        for row in self.con.execute(
            "SELECT phase_id, name, seq FROM phases WHERE run_id=? AND status='success'"
            " AND NOT EXISTS (SELECT 1 FROM events e WHERE e.phase_id = phases.phase_id"
            " AND e.type='log' AND e.name=?)"
            " ORDER BY seq",
            (run_id, INVALIDATED),
        ):
            envelope = self.con.execute(
                "SELECT output_type, payload_json FROM envelopes"
                " WHERE phase_id=? AND valid=1 ORDER BY envelope_id DESC LIMIT 1",
                (row["phase_id"],),
            ).fetchone()
            out[row["name"]] = {
                "phase_id": row["phase_id"],
                "output_type": envelope["output_type"] if envelope else None,
                "payload_json": envelope["payload_json"] if envelope else None,
                "seq": row["seq"],
            }
        return out

    def latest_envelope(self, run_id: str, output_type: str) -> str | None:
        """The latest replay-eligible envelope payload for `output_type`, or None.

        Invalidated phases are ignored — a stale verify/review pass cannot be fed to a
        downstream consumer after its phase was superseded (ticket 132)."""
        row = self.con.execute(
            "SELECT e.payload_json FROM envelopes e"
            " LEFT JOIN phases p ON p.phase_id = e.phase_id"
            " WHERE e.run_id=? AND e.output_type=? AND e.valid=1"
            " AND NOT EXISTS (SELECT 1 FROM events ev WHERE ev.phase_id = p.phase_id"
            "   AND ev.type='log' AND ev.name=?)"
            " ORDER BY e.envelope_id DESC LIMIT 1",
            (run_id, output_type, INVALIDATED),
        ).fetchone()
        return row["payload_json"] if row is not None else None

    def invalidate_from(
        self, run_id: str, name: str, *, reason: str, seq: int | None = None
    ) -> list[str]:
        """Mark the named phase and every later successful phase as no longer replayable.

        A phase can be a success and still be worthless to replay — the run-level
        verdict came in after it.  Rather than flip the row to fail (which
        `PHASE_TRANSITIONS` deliberately forbids), we record a `phase_invalidated`
        log event for each, which `replay_map()` excludes.

        `seq`, when given, is the threshold the caller identified (e.g. the rereview
        target or a stale replay entry): the same name can have two live rows after a
        resume re-ran it, so the latest row of that name is not necessarily the one to
        drop.  Without `seq` the threshold is the latest success row of that name.
        """
        if seq is None:
            row = self.con.execute(
                "SELECT seq FROM phases WHERE run_id=? AND name=? AND status='success'"
                " ORDER BY seq DESC LIMIT 1",
                (run_id, name),
            ).fetchone()
            if row is None:
                return []
            threshold = row["seq"]
        else:
            threshold = seq
        phases = self.con.execute(
            "SELECT phase_id, name, seq FROM phases WHERE run_id=? AND status='success'"
            " AND seq >= ? ORDER BY seq",
            (run_id, threshold),
        ).fetchall()
        dropped: list[str] = []
        for ph in phases:
            self.event(
                run_id,
                "log",
                phase_id=ph["phase_id"],
                name=INVALIDATED,
                payload={"reason": reason, "phase": ph["name"]},
            )
            dropped.append(ph["name"])
        return dropped

    def first_phase_seq(self, run_id: str, name: str) -> int | None:
        """The earliest `seq` a phase of this name ever ran — its logical position
        in the chain. A resume can append later rows of the same name *after* phases
        that are logically downstream of it, so the latest row's seq is not the
        chain position to invalidate from. Read-only."""
        row = self.con.execute(
            "SELECT min(seq) AS seq FROM phases WHERE run_id=? AND name=?", (run_id, name)
        ).fetchone()
        return int(row["seq"]) if row and row["seq"] is not None else None

    def rereview_target(self, run_id: str) -> tuple[str, int] | None:
        """`(name, seq)` of the last live verify before the first live review, or None.

        Live = status success with no `phase_invalidated` event (the `replay_map`
        predicate). Read-only."""
        rows = self.con.execute(
            "SELECT phase_id, name, seq FROM phases WHERE run_id=? AND status='success'"
            " AND NOT EXISTS (SELECT 1 FROM events e WHERE e.phase_id = phases.phase_id"
            " AND e.type='log' AND e.name=?)"
            " ORDER BY seq",
            (run_id, INVALIDATED),
        ).fetchall()
        review = next((r for r in rows if _REVIEW_PHASE.fullmatch(r["name"])), None)
        if review is None:
            return None
        verifies = [
            r for r in rows
            if _VERIFY_PHASE.fullmatch(r["name"]) and r["seq"] < review["seq"]
        ]
        if not verifies:
            return None
        last = verifies[-1]
        return last["name"], int(last["seq"])

    def rereview(self, run_id: str, *, engineer: str) -> list[str]:
        """Drop the live review suffix so the reviewer sees a fresh verify; return the names.

        Decision first, then invalidation: a crash between the two writes leaves the
        target live, so a repeated `--rereview` redoes both."""
        target = self.rereview_target(run_id)
        if target is None:
            return []
        name, seq = target
        self.event(
            run_id,
            "log",
            name=REREVIEW_DECISION,
            payload={
                "reason": "engineer requested --rereview",
                "engineer": engineer,
                "verify": name,
                "seq": seq,
            },
        )
        return self.invalidate_from(
            run_id, name, reason="engineer requested --rereview", seq=seq
        )

    def latest_verify_name(self, run_id: str) -> str | None:
        """The verify the next review or commit relies on; resume re-runs it.

        The name of the highest-`seq` phase row whose name fullmatches `_VERIFY_PHASE`,
        whatever its status (invalidated or not). Read-only."""
        for row in self.con.execute(
            "SELECT name FROM phases WHERE run_id=? ORDER BY seq DESC", (run_id,)
        ):
            if _VERIFY_PHASE.fullmatch(row["name"]):
                return row["name"]
        return None

    def resume_blocked(self, run_id: str) -> str | None:
        """The reason a `--resume` would do zero work, or None when it can proceed.

        Returns None if:
        - the run has no phase rows at all (nothing ran yet)
        - any phase has status='fail'
        - any `phase_invalidated` event exists for the run

        Otherwise every phase succeeded and none was invalidated — build the reason
        from the run's last error event, falling back to a generic message.
        """
        count = self.con.execute(
            "SELECT count(*) FROM phases WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        if count == 0:
            return None
        failed = self.con.execute(
            "SELECT 1 FROM phases WHERE run_id=? AND status='fail' LIMIT 1", (run_id,)
        ).fetchone()
        if failed is not None:
            return None
        invalidated = self.con.execute(
            "SELECT 1 FROM events WHERE run_id=? AND type='log' AND name=? LIMIT 1",
            (run_id, INVALIDATED),
        ).fetchone()
        if invalidated is not None:
            return None
        error = self.con.execute(
            "SELECT name, payload_json FROM events WHERE run_id=? AND type='error'"
            " ORDER BY event_id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if error is not None:
            payload = json.loads(error["payload_json"]) if error["payload_json"] else {}
            reason = payload.get("reason") or error["name"]
            if reason:
                return reason
        return "every phase in the trace already succeeded"

    def add_phase(
        self,
        run_id: str,
        *,
        seq: int,
        name: str,
        kind: str,
        stage: str,
        owner: str | None = None,
        description: str | None = None,
    ) -> str:
        if stage not in STAGES:
            raise ValueError(f"phase {name!r}: unknown stage {stage!r} — one of {STAGES}")
        phase_id = f"{run_id}:{seq:02d}"
        self.con.execute(
            "INSERT INTO phases (phase_id, run_id, seq, name, kind, stage, owner, description)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (phase_id, run_id, seq, name, kind, stage, owner, description),
        )
        self.con.commit()
        return phase_id

    def set_phase_status(self, phase_id: str, status: str, *, error: str | None = None) -> None:
        row = self.con.execute(
            "SELECT status FROM phases WHERE phase_id=?", (phase_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown phase {phase_id!r}")
        old = row["status"]
        if status not in PHASE_TRANSITIONS[old]:
            raise ValueError(f"phase {phase_id}: {old} -> {status} not allowed")
        now = _now()
        retry = old == "fail" and status == "running"
        self.con.execute(
            "UPDATE phases SET status=?,"
            " attempt = attempt + ?,"
            " retries = retries + ?,"
            " error = coalesce(?, error),"
            " started_at = coalesce(started_at, ?),"
            " ended_at = CASE WHEN ? IN ('success','fail') THEN ? ELSE NULL END"
            " WHERE phase_id=?",
            (status, int(retry), int(retry), error, now, status, now, phase_id),
        )
        self.con.commit()

    def start_session(
        self,
        run_id: str,
        phase_id: str,
        *,
        seq: int,
        role: str,
        runtime: str,
        model: str,
        continuation: int = 0,
        session_index: int | None = None,
        color: str | None = None,
        context_window: int | None = None,
        budget_warn: int | None = None,
        budget_stop: int | None = None,
    ) -> str:
        ordinal = session_index if session_index is not None else continuation
        session_id = f"{run_id}:{self._session_name(seq, role, ordinal)}"
        self.con.execute(
            "INSERT INTO sessions (session_id, run_id, phase_id, role, continuation,"
            " runtime, model, color, context_window, created_at, budget_warn, budget_stop)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                run_id,
                phase_id,
                role,
                continuation,
                runtime,
                model,
                color,
                context_window,
                _now(),
                budget_warn,
                budget_stop,
            ),
        )
        self.con.commit()
        session = self.session_dir(run_id, seq, role, continuation, session_index=ordinal)
        (session / "prompts").mkdir(parents=True, exist_ok=True)
        # envelope.json arrives with the Envelope itself; empty JSON would be invalid
        (session / "raw_output.jsonl").touch()
        return session_id

    def touch_session(
        self,
        session_id: str,
        *,
        context_tokens: int | None = None,
        context_window: int | None = None,
    ) -> None:
        """Stamp a session's last use and its current context occupancy."""
        self.con.execute(
            "UPDATE sessions SET last_used_at=?,"
            " context_tokens=coalesce(?, context_tokens),"
            " context_window=coalesce(?, context_window)"
            " WHERE session_id=?",
            (_now(), context_tokens, context_window, session_id),
        )
        self.con.commit()

    def start_summary_session(
        self,
        phase_id: str,
        *,
        role: str,
        runtime: str,
        model: str,
        color: str | None = None,
        budget_warn: int | None = None,
        budget_stop: int | None = None,
    ) -> str:
        """A summarizer Session: a `sessions` row on `NO_RUN`, no Run row and no
        folder, so `lime cost` counts its turns and nothing lists it as a Run."""
        session_id = f"{NO_RUN}:{uuid.uuid4().hex[:12]}-{role}"
        self.con.execute(
            "INSERT INTO sessions (session_id, run_id, phase_id, role, continuation,"
            " runtime, model, color, created_at, budget_warn, budget_stop)"
            " VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                NO_RUN,
                phase_id,
                role,
                runtime,
                model,
                color,
                _now(),
                budget_warn,
                budget_stop,
            ),
        )
        self.con.commit()
        return session_id

    def claim_summary(
        self, key: str, fingerprint: str, *, pid: int | None = None
    ) -> bool:
        """Atomically claim one item for summarization: True when this caller owns
        it, False when an `ok` row already carries this fingerprint or a live pid
        still holds it `working`. A crash mid-claim rolls the reservation back."""
        pid = pid or os.getpid()
        self.con.execute("BEGIN IMMEDIATE")
        try:
            row = self.con.execute(
                "SELECT fingerprint, state, owner_pid FROM summaries WHERE item_key=?",
                (key,),
            ).fetchone()
            if row is not None:
                if row["state"] == "ok" and row["fingerprint"] == fingerprint:
                    self.con.commit()
                    return False
                if row["state"] == "working" and pid_alive(row["owner_pid"]):
                    self.con.commit()
                    return False
            self.con.execute(
                "INSERT INTO summaries (item_key, fingerprint, state, summary, error,"
                " owner_pid, updated_at) VALUES (?, ?, 'working', NULL, NULL, ?, ?)"
                " ON CONFLICT(item_key) DO UPDATE SET fingerprint=excluded.fingerprint,"
                " state='working', summary=NULL, error=NULL,"
                " owner_pid=excluded.owner_pid, updated_at=excluded.updated_at",
                (key, fingerprint, pid, _now()),
            )
        except BaseException:
            self.con.rollback()
            raise
        self.con.commit()
        return True

    def finish_summary(
        self,
        key: str,
        fingerprint: str,
        *,
        summary: str | None,
        error: str | None,
        pid: int | None = None,
    ) -> bool:
        """Record this claim's completion; False when the row moved on — a stale
        or duplicate completion changes nothing."""
        pid = pid or os.getpid()
        state = "ok" if summary else "failed"
        cur = self.con.execute(
            "UPDATE summaries SET state=?, summary=?, error=?, owner_pid=NULL,"
            " updated_at=? WHERE item_key=? AND fingerprint=? AND owner_pid=?"
            " AND state='working'",
            (state, summary if summary else None, error, _now(), key, fingerprint, pid),
        )
        self.con.commit()
        return cur.rowcount == 1

    def record_envelope(
        self,
        run_id: str,
        phase_id: str,
        *,
        agent: str,
        output_type: str,
        payload_json: str,
        valid: bool,
        attempt: int,
    ) -> None:
        self.con.execute(
            "INSERT INTO envelopes (run_id, phase_id, agent, output_type,"
            " payload_json, valid, attempt, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, phase_id, agent, output_type, payload_json, int(valid), attempt, _now()),
        )
        self.con.commit()

    def start_process(
        self,
        run_id: str,
        *,
        kind: str,
        name: str,
        pid: int,
        command: str,
        commit: bool = True,
        retry_of: str | None = None,
    ) -> int:
        cur = self.con.execute(
            "INSERT INTO processes (run_id, kind, name, pid, command, started_at, retry_of)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, kind, name, pid, command, _now(), retry_of),
        )
        if commit:
            self.con.commit()
        return cur.lastrowid

    def end_process(self, run_id: str, pid: int) -> None:
        self.con.execute(
            "UPDATE processes SET ended_at=? WHERE run_id=? AND pid=? AND ended_at IS NULL",
            (_now(), run_id, pid),
        )
        self.con.commit()

    def claim_retry(self, batch_id: str) -> bool:
        """Ticket 255 — the one compare-and-set that decides a dead batch's retry.
        True only for the caller whose UPDATE moved the row; a closed row, a claimed
        row, a retry's own row (retry_of set) or no row returns False and writes nothing."""
        self.con.execute("BEGIN IMMEDIATE")
        try:
            cur = self.con.execute(
                "UPDATE processes SET retried_at=? WHERE run_id=? AND kind='batch'"
                " AND ended_at IS NULL AND retried_at IS NULL AND retry_of IS NULL",
                (_now(), batch_id),
            )
        except BaseException:
            self.con.rollback()
            raise
        self.con.commit()
        return cur.rowcount == 1

    def event(
        self,
        run_id: str,
        type: str,
        *,
        phase_id: str | None = None,
        parent_id: int | None = None,
        name: str | None = None,
        payload: dict | None = None,
        tokens: int | None = None,
    ) -> int:
        """Append one event: db row + a line in events.jsonl (the source of truth).
        A `NO_RUN` row has no Run folder; the db row is the record. Every payload
        string is masked (`lime/mask.py`) before anything is written."""
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type!r}")
        if payload is not None:
            payload = mask_tree(payload)
        started_at = _now()
        cur = self.con.execute(
            "INSERT INTO events (run_id, phase_id, parent_id, type, name,"
            " payload_json, tokens, started_at, seq)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?,"
            " (SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?))"
            " RETURNING event_id, seq",
            (
                run_id,
                phase_id,
                parent_id,
                type,
                name,
                json.dumps(payload) if payload is not None else None,
                tokens,
                started_at,
                run_id,
            ),
        )
        event_id, seq = cur.fetchall()[0]
        self.con.commit()
        line = {
            "event_id": event_id,
            "seq": seq,
            "run_id": run_id,
            "phase_id": phase_id,
            "parent_id": parent_id,
            "type": type,
            "name": name,
            "payload": payload,
            "tokens": tokens,
            "started_at": started_at,
            "ended_at": None,
        }
        if run_id != NO_RUN:
            with (self.run_dir(run_id) / "events.jsonl").open("a") as f:
                f.write(json.dumps(line) + "\n")
        return event_id

    def refusal(
        self, ticket: str | None, reason: str, *, run_id: str | None = None, **detail
    ) -> int:
        """Record one refusal as a `refusal` event: `name` is the reason, `payload`
        carries `{"ticket": <stem>, **detail}`, `run_id` is the Run's id or `NO_RUN`
        (`"-"`) when no Run row exists yet."""
        return self.event(run_id or NO_RUN, "refusal", name=reason, payload={"ticket": ticket, **detail})

    def set_pr(self, run_id: str, number: int | None, url: str | None) -> None:
        """The only writer of the `pr_number`/`pr_url` pair; idempotent (a re-run
        rewrites the same values), and never backfilled for older Runs."""
        self.con.execute(
            "UPDATE runs SET pr_number=?, pr_url=? WHERE run_id=?",
            (number, url, run_id),
        )
        self.con.commit()

    def add_totals(self, run_id: str, *, tokens: int, cost: float) -> None:
        """Roll one Session's final usage into the run row, at agent end."""
        self.con.execute(
            "UPDATE runs SET total_tokens = total_tokens + ?,"
            " total_cost = total_cost + ? WHERE run_id=?",
            (tokens, cost, run_id),
        )
        self.con.commit()

    def record_gate(
        self,
        run_id: str,
        phase_id: str,
        *,
        attempt: int,
        gate: str,
        passed: bool,
        violations: list | None = None,
        checks: list | None = None,
    ) -> None:
        self.con.execute(
            "INSERT INTO gate_results (run_id, phase_id, attempt, gate, passed,"
            " violations_json, checks_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                phase_id,
                attempt,
                gate,
                int(passed),
                json.dumps(violations) if violations is not None else None,
                json.dumps(checks) if checks is not None else None,
                _now(),
            ),
        )
        self.con.commit()
