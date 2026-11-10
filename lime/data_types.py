"""The one agent contract (spec §4), the Envelopes (spec §6), and the phase types.

RULE (four-param rule): any function that takes more than 4 parameters
takes ONE of these objects instead. AgentCall and PhaseParams are the pattern.

Every agent call declares a concrete output type — an EnvelopeBase subclass —
that its final JSON response is parsed against. No untyped handoffs.
"""

from typing import Callable, Literal, Optional, Type

from pydantic import BaseModel, Field, ValidationInfo, field_validator

DEFAULT_TIMEOUT_S = 1800

# One builder `bash` call's deadline in seconds (ticket 198). Above verify's *default*
# slowest command cap (quality.TIMEOUTS, 600 s) so a legitimate suite never trips it; at
# most half the Session ceiling so a hung command costs at most half a Session. A
# project's `commands.test_timeout` may exceed it, because verify and `lime deploy`'s
# merged-tree check run the suite in lime's own process, not in a builder `bash` call.
# guard.ts caps the call, pi kills the process tree, and the Session takes its next turn.
BASH_DEADLINE_S = 900

Runtime = Literal["pi", "claude_code"]


class CostBreakdown(BaseModel):
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    total: float = 0.0


class UsageBreakdown(BaseModel):
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    total: int = 0
    cost: CostBreakdown = Field(default_factory=CostBreakdown)

    def merge(self, other: "UsageBreakdown") -> None:
        """Add another call's usage — a phase that retries spends more than once."""
        for field in ("input", "output", "cache_read", "cache_write", "reasoning", "total"):
            setattr(self, field, getattr(self, field) + getattr(other, field))
        for field in ("input", "output", "cache_read", "cache_write", "total"):
            setattr(self.cost, field, getattr(self.cost, field) + getattr(other.cost, field))


class AgentConfig(BaseModel):
    """One `agents:` entry after the defaults/profile merge (spec §5)."""

    name: str
    purpose: str = ""
    color: str = ""
    prompt_engineering: dict[str, str] = Field(default_factory=dict)
    harness_engineering: list[str] = Field(default_factory=list)
    runtime: Runtime = "pi"
    model: str
    thinking: str = "medium"
    tool_choice: Literal["auto", "required"] = "auto"
    tools: list[str] = Field(default_factory=list)
    writes: list[str] | None = None  # None = unrestricted minus trust; [] = read-only
    budget: dict[str, int] | None = None  # {warn, stop} override — unused in the template (spec §9)
    timeout_s: int = DEFAULT_TIMEOUT_S  # one Session's wall-clock ceiling in seconds; profile-patchable (ticket 82)


class AgentRequest(BaseModel):
    runtime: Runtime
    model: str
    thinking: str = "medium"
    system_prompt: str
    prompt: str
    session_id: str  # the same id re-enters the same context (create-or-continue)
    resume: bool = False
    session_dir: str
    raw_output_path: str
    cwd: str
    tools: list[str] | None = None  # runtime-native names; None = runtime default set; [] = no tools
    # The Role's write boundary, exported to the child so the Guard can fence writes
    # at call time (not just phase-end). None = unset (no call-time restriction beyond
    # the phase-end diff check); [] = read-only; [...] = only these paths.
    writes: list[str] | None = None
    guards: list[str] = Field(default_factory=list)
    max_turns: int | None = None
    timeout_s: int = DEFAULT_TIMEOUT_S
    attachments: list[str] = Field(default_factory=list)
    # The Guard's mailbox (spec §8, §9) — set by agents.execute(), read by the
    # adapters to env the spawned process so `lime guard`/`guard.ts` find them
    # without threading a fifth parameter through the runtime contract.
    budget_file: str = ""
    handoff_dir: str = ""
    handoff_path: str = ""  # the exact file a budget-stop continuation should read
    tool_choice: Literal["auto", "required"] = "auto"
    submit_schema: str = ""  # pi: path of the role's envelope JSON Schema; submit.ts reads it via LIME_SUBMIT_SCHEMA (ticket 74)
    bash_deadline_s: int | None = None  # pi: cap on each `bash` call, exported as LIME_BASH_DEADLINE_S for guard.ts (ticket 198); None = no cap
    # pi: lime's own `model_validate` verdict for a `submit`'s raw args (ticket 250) — the
    # `tool_result`'s `ok` reports whether lime accepts the envelope, not pi's harness flag.
    # None = no verdict available; the adapter falls back to harness acceptance.
    envelope_validator: Callable[[dict], bool] | None = Field(default=None, exclude=True)
    should_stop: Callable[[], bool] | None = Field(default=None, exclude=True)  # orchestrator's per-Session stop (ticket 173): True → end like the time ceiling


class AgentResult(BaseModel):
    status: Literal["ok", "max_turns", "denied", "error", "timeout"]
    text: str = ""
    session_id: str = ""
    returncode: int = 0
    usage: UsageBreakdown = Field(default_factory=UsageBreakdown)
    cost: float = 0.0
    context_tokens: int = 0  # input + cache_read + cache_write of the last assistant message
    context_window: int = 0
    denials: list[dict] = Field(default_factory=list)
    stderr_tail: str = ""
    error: str = ""  # the runtime's own reason for a run that ended without an answer (pi: the final assistant message's errorMessage)
    has_tool_calls: bool = False  # pi: the last assistant message contained structured toolCall content parts
    envelope: dict | None = None  # pi: args of the last accepted `submit` call — the envelope itself; None = no call (claude_code always None until its MCP follow-up)
    submit_error: str = ""  # pi: text of the last `submit` call pi rejected at the schema (ticket 78); "" when none — read only when `envelope` is None


class EnvelopeBase(BaseModel):
    status: Literal["success", "fail", "partial"]
    summary: str = ""
    artifacts: list[str] = Field(default_factory=list)
    notes_for_next_agent: str = ""


class GenericOutput(EnvelopeBase):
    pass


class PlanOutput(EnvelopeBase):
    """The plan Envelope, without a `commit_message` — the plan lands in context_handoff/, no plan commit (§6)."""


class BuildOutput(EnvelopeBase):
    changed_files: list[str] = Field(default_factory=list)
    commit_message: str = ""  # consumed by the git commit phase


class ScoutFinding(BaseModel):
    file: str
    note: str = ""


class ScoutOutput(EnvelopeBase):
    findings: list[ScoutFinding] = Field(default_factory=list)


class ReviewFinding(BaseModel):
    """One thing the request (or plan) asked for, and whether it is there."""

    requirement: str  # the ask, in the requester's words
    met: bool
    evidence: str = ""  # where it lives, or what is missing


class ReviewOutput(EnvelopeBase):
    """Confirmation that what was built is what was asked for — not a test run."""

    approved: bool = False
    findings: list[ReviewFinding] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list)  # what must change before approval
    # lime (§6): `file:line — what, which never-rule`. Never approved while non-empty.
    trust_violations: list[str] = Field(default_factory=list)
    # pre-192 envelopes may carry a `layout` key; extra keys are ignored on load, so they replay unchanged.


class DocumentOutput(EnvelopeBase):
    """lime shape (§6): all empty = nothing to document, still success."""

    changed_docs: list[str] = Field(default_factory=list)
    adrs: list[str] = Field(default_factory=list)
    commit_message: str = ""  # docs commit subject; empty when nothing changed


class ResearchSource(BaseModel):
    url: str
    note: str = ""  # what this source settled


class ResearchOutput(EnvelopeBase):
    sources: list[ResearchSource] = Field(default_factory=list)


class DesignVariant(BaseModel):
    id: str  # a | b | c — the ?variant= value
    note: str = ""  # one line: what makes this one different


class DesignOutput(EnvelopeBase):
    route_dir: str = ""
    url_path: str = ""
    variants: list[DesignVariant] = Field(default_factory=list)
    notes_path: str = ""  # empty until `lime prototype --lock` writes NOTES.md


class DiffCapContributor(BaseModel):
    """One path's added-lines contribution — used for both top counted
    contributors and the existing per-file uncounted list (ticket 130)."""

    path: str
    added_lines: int


PLAN_PROVENANCE_VERSION = 2


class AccountingPolicy(BaseModel):
    """The canonical, effective diff-cap policy a verify was measured under.

    Defaults and explicitly-equivalent defaults must compare equal: exclude and
    uncounted are sorted/deduplicated, so `["context_handoff/"]` (the default)
    and an explicit `["context_handoff/"]` produce the same policy.
    """

    max_added_lines: int = 1500
    exclude: list[str] = Field(default_factory=list)
    uncounted: list[str] = Field(default_factory=list)
    accounting_version: int = 0


class PlanProvenance(BaseModel):
    """Inputs the planner actually saw, so an edited ticket cannot silently resume
    a stale plan (ticket 132, 129).

    `ticket_sha256` is over the authoritative original ticket bytes; `prompt` is the
    exact augmented planner prompt (the ticket/bug-fix input plus the resolved builder
    limits line); `plan_sha256` is over the produced plan.md bytes. A caller with no
    ticket identity records an empty `ticket_sha256`, but the prompt/limits and plan
    bytes are always compared. Version 2 adds the resolved-limits input to the
    identity contract.
    """

    version: int = PLAN_PROVENANCE_VERSION
    ticket_sha256: str = ""
    prompt: str = ""
    plan_sha256: str = ""


class AccountingMeasurement(BaseModel):
    """One read-only diff-cap measurement — the shared result verify consumes (ticket 132).

    `failures` carries ordinary gate failures only (binary, inspection error); size
    never fails.
    """

    requested_base: str = ""
    head_sha: str = ""
    merge_base_sha: str = ""
    exclude: list[str] = Field(default_factory=list)
    uncounted: list[str] = Field(default_factory=list)
    max_added_lines: int = 0
    counted_total: int = 0
    uncounted_total: int = 0
    eligible_total: int = 0
    counted_files: list[DiffCapContributor] = Field(default_factory=list)
    eligible_files: list[DiffCapContributor] = Field(default_factory=list)
    uncounted_files: list[DiffCapContributor] = Field(default_factory=list)
    binaries: list[str] = Field(default_factory=list)
    top_contributors: list[DiffCapContributor] = Field(default_factory=list)
    omitted_contributors: int = 0
    failures: list[str] = Field(default_factory=list)
    inspection_error: str = ""


class VerifyCheck(BaseModel):
    """One quality command's result as the Spine recorded it on the verify envelope (ticket 184) — no output tail."""

    name: str
    command: str
    returncode: int
    passed: bool
    duration_seconds: float


class VerifyOutput(EnvelopeBase):
    """A deterministic result, shaped as an envelope so an agent can consume it.

    Agents hand each other typed envelopes; code blocks return QualityResult.
    This is the adapter, so a failing lint or test run flows back into the
    builder through exactly the same door an agent's report would — the chain
    script is the only thing that knows the difference.
    """

    passed: bool = False
    failures: list[str] = Field(default_factory=list)
    # full HEAD sha the verify measured, for the reviewer's `verified HEAD`; "" on legacy envelopes or a failed HEAD read
    head_sha: str = ""
    # pre-193 envelopes may still carry the deleted overflow/warnings keys; extra
    # keys are ignored on load, so those recorded envelopes replay unchanged.
    # every quality command's result, recorded by the Spine (ticket 184); empty on
    # legacy envelopes, which still load and replay
    checks: list[VerifyCheck] = Field(default_factory=list)


class ChangesOutput(EnvelopeBase):
    """A ChangeSet shaped as an envelope so an agent can be handed it directly.

    Same adapter idea as VerifyOutput: code computes the diff, the documenter
    consumes it through the one door every agent handoff uses.
    """

    base: str = ""  # "<ref> @ <commit> — <reason>"
    changed_files: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    stat: str = ""
    diff_path: str = ""  # read this for the full diff


# --- Phases ---

PhaseKind = Literal["engineer", "agent", "code"]
PhaseStage = Literal["Prepare", "Build", "Review", "Ship"]
PhaseStatus = Literal["queued", "running", "success", "fail"]


class PhaseParams(BaseModel):
    """Everything run.phase() needs. Passed as one object, never loose params."""

    name: str  # short id, unique within the run: "plan", "build"
    kind: PhaseKind  # which lane the block renders in
    stage: PhaseStage  # which stage row the Run canvas draws it in (owner table, ticket 251)
    owner: str  # engineer's name, "git", or an agent name from config
    description: str  # REQUIRED: what this phase does and why — see below
    retries: int = 0  # agent phases: gate-failure retries via continue
    fatal: bool = True  # False: an Exception fails only this Phase — the Run stays open and finish() ignores it (ticket 197)

    @field_validator("description")
    @classmethod
    def _description_must_be_earned(cls, value: str, info: ValidationInfo) -> str:
        """A phase name identifies; a description explains. Both are required.

        The description is the only sentence the trace, the console, and the
        phase block in the UI ever show about intent — everything else is ids,
        statuses, and timings. `commit_plan: "Commit the plan"` tells a reader
        nothing they could not already see, so an echo is rejected the same way
        a blank one is. This is a construction-time error on purpose: it fires
        before the phase opens, not after a run is already in the trace.
        """
        text = " ".join(value.split())
        name = str(info.data.get("name", "?"))
        if not text:
            raise ValueError(
                f"phase {name!r}: description is required — one sentence on what this"
                " phase does and why. It is what the trace and the UI show."
            )
        if text.rstrip(".").casefold() == name.replace("_", " ").casefold():
            raise ValueError(
                f"phase {name!r}: description {text!r} only restates the phase name —"
                " say what it does and why instead."
            )
        return text


class Phase(BaseModel):
    """The in-memory phase record — PhaseParams plus lifecycle (rows live in the tracer)."""

    phase_id: str
    run_id: str
    seq: int
    params: PhaseParams
    status: PhaseStatus = "fail"  # success must be earned
    attempt: int = 0
    error: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


# --- Deterministic quality blocks ---


class QualityCheckSpec(BaseModel):
    """One deterministic quality command.

    lime: the command is the project's own shell line from `config.yaml
    commands:` (ADR 0001), not an argv list hard-coded per repo.
    """

    name: str  # the config key: test | lint | typecheck | build
    command: str  # shell line, verbatim from config
    timeout_seconds: int = 120


class QualityCheckResult(VerifyCheck):
    """Captured evidence from one quality command."""

    output_artifact: str
    # The tail of stdout+stderr, verbatim and unparsed. A failure has to travel
    # back to the builder as an envelope, and the builder cannot open a log file
    # it was never handed — so the evidence rides along. Deliberately raw: every
    # runner formats failures differently and a generic parser would be
    # confidently wrong. The full log is always at output_artifact.
    output_tail: str = ""


class QualityResult(BaseModel):
    """Aggregate result from a quality block: every check it ran, and the verdict."""

    passed: bool
    checks: list[QualityCheckResult] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


# --- Change capture (git diff, deterministic) ---


class ChangeCapture(BaseModel):
    """Everything changes.capture() needs. One object, never loose params."""

    base: str = "main"  # the ref the work is measured against
    max_diff_lines: int = 2000  # the diff artifact is truncated past this
    include_untracked: bool = True  # a brand-new file is part of the change


class BaseRef(BaseModel):
    """The commit a change is measured from, and why that one.

    `reason` is the line the trace shows. A diff is only as trustworthy as the
    thing it was taken against, so the chain records that choice instead of
    leaving the reader to infer it.
    """

    ref: str  # what was asked for: "main", or a pinned sha
    commit: str  # the commit actually diffed against
    reason: str = ""

    @property
    def label(self) -> str:
        """Display form — a named ref as itself, a pinned raw sha shortened."""
        if len(self.ref) == 40 and all(c in "0123456789abcdef" for c in self.ref):
            return self.ref[:7]
        return self.ref


class ChangeSet(BaseModel):
    """What changed since the base commit — pure git facts, no judgement."""

    base: BaseRef
    files: list[str] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    stat: str = ""  # `git diff --stat` output, verbatim
    diff_path: str = ""  # the full diff, written into context_handoff/
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not (self.files or self.untracked)


# --- Agent calls ---


class GateCheck(BaseModel):
    """One thing a gate looked at, and what it found.

    `note` is the evidence — "exists, 2.1KB", "exit 0", "not in the diff". On a
    failed check it doubles as the reason, so it is what the agent is told.
    """

    item: str  # what was checked: a path, a command, a test
    ok: bool
    note: str = ""


class GateReport(BaseModel):
    """What every gate returns: the checks it ran. Violations are derived.

    Authoring stays a one-liner per item — `report.check(...)` appends and
    returns self, so a gate is a loop and a return.
    """

    checks: list[GateCheck] = Field(default_factory=list)

    def check(self, item: str, ok: bool, note: str = "") -> "GateReport":
        self.checks.append(GateCheck(item=item, ok=ok, note=note))
        return self

    @property
    def violations(self) -> list[str]:
        return [f"{c.item}: {c.note or 'failed'}" for c in self.checks if not c.ok]

    @property
    def passed(self) -> bool:
        return not self.violations


class AgentCall(BaseModel):
    """One agent invocation: prompt in, typed envelope out, gates verified."""

    model_config = {"arbitrary_types_allowed": True}

    output_type: Type[EnvelopeBase]
    prompt: str
    previous: Optional[EnvelopeBase] = None
    gates: list[Callable] = Field(default_factory=list)  # gate(envelope, run) -> GateReport
    attachments: list[str] = Field(default_factory=list)  # images/SVGs for the designer (ticket 17)
    max_turns: int | None = None  # spawn cap; None = runtime default. No phase sets one — the token budget is the ceiling (ticket 72)


class BuildStep(BaseModel):
    """One Step, one attempt — what `phases.build()` needs (four-param rule, §1)."""

    step_no: int
    prompt: str
    previous: Optional[EnvelopeBase] = None
    attempt: int = 1  # > 1 names the retry after a red verify (spec §7)


class ReviewStep(BaseModel):
    """One review, one attempt — what `phases.review()` needs (four-param rule, §1)."""

    prompt: str
    checklist: str  # the ticket's `Done means`, verbatim — the `## Checklist` section
    base: str  # the default branch the diff is measured against
    previous: Optional[EnvelopeBase] = None
    attempt: int = 1  # > 1 names the re-review after a revise
