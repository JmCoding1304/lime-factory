"""Session lifecycle: pin-or-create a run_id, build the Run object.

`ensure(cfg, run_id, ...)` joins the Run if its row exists or creates it under
exactly that id (pinned ids for repeatable runs); omitted, a fresh id is
minted and printed so the next chain can pick it up.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from .runner import KilledBySignal, Run
from .tracer import Tracer, start_heartbeat
from .utils import engineer_name, new_id

RUNS_DIR = Path(".lime/runs")  # fixed under the target project (spec §2)


def _finalize_when_killed(run: Run) -> None:
    """A killed run still closes its own trace.

    Python's default SIGTERM handling exits without unwinding, so a
    `kill <pid>` would leave the run reading `running` forever and its process
    rows open — the trace would claim work is in flight that is already dead.
    Turning the signal into SystemExit both finalizes here and lets the phase
    context manager record the phase as failed on the way out. A closed terminal
    (SIGHUP) closes the Run as failed too, instead of leaving it `running`.
    """

    def handler(signum, _frame):
        run.close_run(ok=False)  # also closes process rows
        raise KilledBySignal(signum)  # str() is the trace's reason; .code is the shell's status

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, handler)


def ensure(
    cfg: dict,
    run_id: str | None = None,
    *,
    workflow: str = "feature",
    request: str = "",
    kind: str = "run",
    profile: str | None = None,
    never: str = "",
    runs_dir: Path | str = RUNS_DIR,
    ticket: str | None = None,
    branch: str | None = None,
    worktree: str | None = None,
    port: int | None = None,
    ports: list[int] | None = None,
    db_name: str | None = None,
    base_sha: str | None = None,
    batch_id: str | None = None,
) -> Run:
    run_id = run_id or new_id(8)
    tracer = Tracer(Path(runs_dir))
    engineer = engineer_name()
    run_base: str | None = None
    if not tracer.run_exists(run_id):
        port = tracer.start_run(
            run_id,
            workflow=workflow,
            request=request,
            kind=kind,
            engineer=engineer,
            ticket=ticket,
            branch=branch,
            worktree=worktree,
            port=port,
            ports=ports,
            db_name=db_name,
            profile=profile,
            base_sha=base_sha,
            batch_id=batch_id,
        )
        run_base = base_sha
    else:
        # A Run's birth identity is immutable: hydrate it from the stored row and
        # never from current HEAD or an incoming replacement value. A legacy NULL
        # stays NULL — that is legacy behavior, not an inferred base.
        stored = tracer.con.execute(
            "SELECT base_sha FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        run_base = stored["base_sha"] if stored is not None else None
        if port is None:
            row = tracer.con.execute(
                "SELECT port FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            port = row["port"] if row is not None else None
    run = Run(
        cfg=cfg, run_id=run_id, tracer=tracer, engineer=engineer,
        profile=profile, never=never, worktree=worktree, base_sha=run_base,
    )
    run.port = port  # the Run's own port (spec §7) — set fresh or looked up when joining
    # This process is the run. Record it before any phase opens, so a run that
    # hangs in its first agent call is still killable by run_id.
    process_id = tracer.start_process(
        run_id,
        kind="run",
        name=Path(sys.argv[0]).stem,
        pid=os.getpid(),
        command=" ".join([Path(sys.argv[0]).name, *sys.argv[1:]]),
    )
    start_heartbeat(tracer.runs_dir / "lime.db", process_id)
    _finalize_when_killed(run)
    run.console.session_started(run_id, engineer)
    return run
