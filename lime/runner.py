"""The Run object: config + run_id + agent roster + tracer + console, bound once.

`run.phase(PhaseParams(...))` is the ONE phase primitive — a context manager
for all three kinds (engineer, agent, code). Success must be earned: every
phase defaults to fail; only a clean exit flips it (agent phases additionally
require a parsed envelope + green gates, enforced inside ph.call). A `fatal=False`
Phase's Exception fails that Phase only — the Run stays open.
"""

from __future__ import annotations

import json
import signal
import time
from collections.abc import Iterable
from contextlib import contextmanager

from . import config, git_helper
from .console import Console
from .data_types import AgentCall, AgentConfig, EnvelopeBase, Phase, PhaseParams
from .utils import ensure_dir, now_iso


class KilledBySignal(SystemExit):
    """SIGTERM/SIGINT turned into an exit (ticket 57): str() is the trace's reason,
    `.code` the shell's status, `.signal` the name the crash_detected event carries."""

    def __init__(self, signum: int):
        self.signal = signal.Signals(signum).name
        super().__init__(f"killed by {self.signal}")
        self.code = 128 + signum


class PhaseHandle:
    def __init__(self, run: "Run", phase: Phase):
        self.run = run
        self.phase = phase

    def log(self, **payload) -> None:
        self.run.tracer.event(
            self.run.run_id,
            "log",
            phase_id=self.phase.phase_id,
            name=self.phase.params.name,
            payload=payload,
        )
        self.run.console.note(", ".join(f"{k}: {v}" for k, v in payload.items()))

    def call(self, call: AgentCall) -> EnvelopeBase:
        if self.phase.params.kind != "agent":
            raise RuntimeError("ph.call() is only valid inside an agent phase")
        from . import agents  # here, not at module top: agents imports runner's callers' world

        return agents.execute(self.run, self.phase, call)


class Run:
    def __init__(
        self,
        cfg: dict,
        run_id: str,
        tracer,
        engineer: str,
        profile: str | None = None,
        never: str = "",
        worktree: str | None = None,
        base_sha: str | None = None,
    ):
        self.worktree = worktree  # the Run's own tree; None = main tree shared with the engineer
        self.base_sha = base_sha  # the immutable birth identity — full HEAD pinned at launch; None = legacy
        self.cfg = cfg  # the raw config.yaml dict (§5)
        self.commands = cfg.get("commands") or {}
        self.agents = {a.name: a for a in config.roster(cfg, profile)}
        self.profile = profile
        self.never = never  # trust.yaml rendered as the "Never …" block (ticket 26 feeds it)
        self.run_id = run_id
        self.tracer = tracer
        self.console = Console(tracer, run_id)
        self.engineer = engineer
        self.phases: list[Phase] = []
        self.replay: dict[str, dict] = {}  # --resume only: tracer.replay_map — successes to skip
        self.tokens = 0
        self.cost = 0.0
        self._seq = tracer.max_phase_seq(run_id)  # a joined run continues the sequence
        self.repo_root = git_helper.repo_root()  # where every agent is spawned to work
        self.run_dir = ensure_dir(tracer.run_dir(run_id))
        self.context_handoff_dir = ensure_dir(self.run_dir / "context_handoff")
        self._agent_map_path = self.run_dir / "agent_map.json"
        self.agent_map: dict = (
            json.loads(self._agent_map_path.read_text())
            if self._agent_map_path.exists()
            else {}
        )
        self._run_closed = False  # the runs row transitions exactly once
        self.clock = time.monotonic  # the time ceiling's clock (agents.py); tests swap in a fake

    def agent(self, name: str) -> AgentConfig:
        try:
            return self.agents[name]
        except KeyError:
            raise RuntimeError(
                f"agent {name!r} is not in the roster — available: {sorted(self.agents)}"
            ) from None

    # --- agent map (run_id -> per-agent runtime session ids) ---
    def save_agent_map(self, agent: str, entry: dict) -> None:
        self.agent_map[agent] = entry
        self._agent_map_path.write_text(json.dumps(self.agent_map, indent=2))

    def close_agent_session(self, agent: str) -> None:
        """Mark the agent's entry as closed; repeated calls are harmless, missing roles are a no-op."""
        entry = self.agent_map.get(agent)
        if entry is None:
            return
        self.save_agent_map(agent, {**entry, "closed": True})

    # --- usage (run totals mirror what the tracer accumulates in sqlite) ---
    def add_usage(self, tokens: int, cost: float) -> None:
        self.tokens += tokens
        self.cost += cost
        self.tracer.add_totals(self.run_id, tokens=tokens, cost=cost)

    # --- resolved roster (ticket 84) ---
    def roster_rows(self, roles: Iterable[str] | None = None) -> list[dict]:
        """runtime/model/thinking per role \u2014 the profile-patchable keys, what a resume can change (ticket 84)."""
        names = list(roles) if roles is not None else list(self.agents)
        return [{"name": n, "runtime": a.runtime, "model": a.model, "thinking": a.thinking}
                for n in names if (a := self.agents.get(n)) is not None]

    def announce_roster(self, roles: Iterable[str] | None = None) -> None:
        """Print the resolved roster and store it in run.json; a rejoin shows what changed since the card was written."""
        card = self.tracer.card(self.run_id)
        previous = {r["name"]: r for r in card["roster"]} if "roster" in card else None
        rows = self.roster_rows(roles)
        self.console.roster(rows, previous)
        self.tracer.update_card(self.run_id, roster=rows)

    def close_run(self, ok: bool = True, *, status: str | None = None) -> None:
        """Settle the runs row exactly once — crash path and finish() both land here.

        `status` overrides the plain success/fail mapping for a Run-level outcome
        the caller already knows more precisely than ok/not-ok — `budget_exhausted`
        (spec §9) when a Phase burns its one continuation. Whichever call gets here
        FIRST wins: a generic `close_run(ok=False)` from the phase() crash handler,
        arriving after budget.py already closed the run as `budget_exhausted`, is a
        no-op — the more specific status sticks.
        """
        if self._run_closed:
            return
        self._run_closed = True
        self.tracer.set_run_status(self.run_id, status or ("success" if ok else "fail"))

    # --- the phase primitive ---
    @contextmanager
    def phase(self, params: PhaseParams):
        self._seq += 1
        phase_id = self.tracer.add_phase(
            self.run_id,
            seq=self._seq,
            name=params.name,
            kind=params.kind,
            stage=params.stage,
            owner=params.owner,
            description=params.description,
        )
        self.tracer.set_phase_status(phase_id, "running")
        phase = Phase(
            phase_id=phase_id,
            run_id=self.run_id,
            seq=self._seq,
            params=params,
            status="running",
            started_at=now_iso(),
        )
        self.phases.append(phase)
        self.tracer.event(
            self.run_id,
            "phase_start",
            phase_id=phase_id,
            name=params.name,
            payload={
                "kind": params.kind,
                "owner": params.owner,
                "description": params.description,
            },
        )
        self.console.phase_started(phase)
        clock = time.monotonic()
        try:
            yield PhaseHandle(self, phase)
        except BaseException as error:
            # A non-fatal Phase tolerates an ordinary Exception: it fails only this
            # Phase, and the Run is left open (ticket 197). A KilledBySignal/
            # KeyboardInterrupt is a BaseException, not an Exception, so it still
            # closes the Run.
            tolerated = not params.fatal and isinstance(error, Exception)
            phase.status = "fail"  # success must be earned
            phase.error = str(error)[:1000]
            phase.ended_at = now_iso()
            self.tracer.event(
                self.run_id,
                "error",
                phase_id=phase_id,
                name=params.name,
                payload={"error": phase.error},
            )
            if isinstance(error, KilledBySignal):
                self.tracer.event(
                    self.run_id,
                    "crash_detected",
                    phase_id=phase_id,
                    name=params.name,
                    payload={"signal": error.signal, "exit_code": error.code},
                )
            self.tracer.event(
                self.run_id,
                "phase_end",
                phase_id=phase_id,
                name=params.name,
                payload={"status": "fail"},
            )
            self.tracer.set_phase_status(phase_id, "fail", error=phase.error)
            if not tolerated:
                self.close_run(ok=False)
            self.console.phase_ended(phase, time.monotonic() - clock)
            if not tolerated:
                self.console.session_finished(
                    False, self.tokens, self.cost, str(self.tracer.runs_dir / "lime.db"),
                )
            raise
        else:
            phase.status = "success"
            phase.ended_at = now_iso()
            self.tracer.event(
                self.run_id,
                "phase_end",
                phase_id=phase_id,
                name=params.name,
                payload={"status": "success"},
            )
            self.tracer.set_phase_status(phase_id, "success")
            self.console.phase_ended(phase, time.monotonic() - clock)

    # --- run outcome ---
    def finish(self, accepted: bool = True, reason: str = "") -> int:
        """Finalize the run and return its exit code. Call this exactly once.

        Two criteria, not one. Every phase must have passed, AND the chain's own
        acceptance test must hold. They are different questions on purpose: a
        test phase that ran the suite did its job even when the suite came back
        red, so the PHASE succeeds while the RUN must not. One call settles the
        db, the banner, and the exit code together, so the three cannot disagree.
        A non-fatal Phase's failure stays in the trace but does not sink the Run.
        """
        phases_ok = bool(self.phases) and all(
            p.status == "success" or not p.params.fatal for p in self.phases
        )
        ok = phases_ok and accepted
        if phases_ok and not accepted:
            note = reason or "the run's acceptance criterion was not met"
            self.tracer.event(
                self.run_id,
                "error",
                phase_id=self.phases[-1].phase_id if self.phases else None,
                name="not_accepted",
                payload={"reason": note},
            )
            self.console.note(f"not accepted: {note}")
        self.close_run(ok=ok)
        self.console.session_finished(
            ok, self.tokens, self.cost, str(self.tracer.runs_dir / "lime.db"),
        )
        return 0 if ok else 1
