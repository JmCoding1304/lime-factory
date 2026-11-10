import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type {
  Envelope,
  EventRow,
  FeedEvent,
  FeedRun,
  GateResult,
  Phase,
  Run,
  Session,
} from "./types";
import { phaseSignals } from "./signals";
import { phaseError, runError } from "./failure";
import { phaseCost } from "./cash";
import {
  cardDetail,
  diffFact,
  gateChecks,
  phaseChecks,
  phaseOutcome,
  repoPath,
  type CardDetailView,
  type DetailFact,
} from "./cardDetail";
import { feedRows } from "./runLine";
import { fmtClock, fmtDuration } from "./format";

// ── Builders ────────────────────────────────────────────────────────────────

function ev(over: Partial<EventRow>): EventRow {
  return {
    rowid: 1,
    event_id: 1,
    run_id: "r1",
    phase_id: "p1",
    parent_id: null,
    type: null,
    name: null,
    payload_json: null,
    tokens: null,
    started_at: null,
    ended_at: null,
    ...over,
  };
}

function session(id: string, phaseId: string, model: string | null): Session {
  return {
    session_id: id,
    run_id: "r1",
    phase_id: phaseId,
    role: "builder",
    continuation: 0,
    runtime: "pi",
    model,
    color: null,
    context_tokens: null,
    context_window: null,
    created_at: null,
    last_used_at: null,
  };
}

function phase(over: Partial<Phase>): Phase {
  return {
    phase_id: "p1",
    run_id: "r1",
    seq: 1,
    name: "build_1",
    kind: "code",
    owner: "eng",
    description: null,
    status: "success",
    attempt: 1,
    retries: 0,
    error: null,
    started_at: null,
    ended_at: null,
    ...over,
  };
}

/** A signal-ish event carrying a JSON payload. */
function sig(
  type: EventRow["type"],
  name: string | null,
  payload: unknown,
): EventRow {
  return ev({
    type,
    name,
    payload_json: payload === undefined ? null : JSON.stringify(payload),
  });
}

/** A turn event attributed to a session id with a cost. */
function turn(name: string, cost: number): EventRow {
  return sig("turn", name, { cost });
}

// ── phaseSignals ─────────────────────────────────────────────────────────────

describe("phaseSignals", () => {
  test("budget_warn reads its numbers and tones warn", () => {
    const out = phaseSignals([
      sig("budget_warn", "builder", { context_tokens: 151000, warn: 150000 }),
    ]);
    expect(out).toEqual([
      {
        type: "budget_warn",
        glyph: "▲",
        tone: "warn",
        text: "context 151k passed the 150k warn line",
      },
    ]);
  });

  test("budget_stop reads its numbers and tones bad", () => {
    const out = phaseSignals([
      sig("budget_stop", "builder", { context_tokens: 240000, stop: 250000 }),
    ]);
    expect(out).toEqual([
      {
        type: "budget_stop",
        glyph: "■",
        tone: "bad",
        text: "budget stop at 240k (stop 250k)",
      },
    ]);
  });

  test("timeout_kill formats elapsed and marks a repeat", () => {
    expect(phaseSignals([sig("timeout_kill", "builder", { elapsed_s: 900 })])).toEqual([
      { type: "timeout_kill", glyph: "⏱", tone: "bad", text: "timeout kill after 15m 00s" },
    ]);
    expect(
      phaseSignals([
        sig("timeout_kill", "builder", { elapsed_s: 900, reason: "repeat" }),
      ]).at(0)?.text,
    ).toBe("timeout kill after 15m 00s (repeat)");
  });

  test("crash_detected reads signal and exit", () => {
    expect(
      phaseSignals([sig("crash_detected", "builder", { signal: 9, exit_code: -9 })]),
    ).toEqual([
      { type: "crash_detected", glyph: "✕", tone: "bad", text: "crash · signal 9 · exit -9" },
    ]);
  });

  test("continuation joins the fields it has", () => {
    expect(
      phaseSignals([
        sig("continuation", "builder", {
          continuation: 1,
          reason: "response_stalled",
          error: "boom",
        }),
      ]),
    ).toEqual([
      {
        type: "continuation",
        glyph: "↻",
        tone: "warn",
        text: "fresh session 1 · response_stalled · boom",
      },
    ]);
  });

  test("needs_rescope handoff is the only handoff that renders", () => {
    expect(
      phaseSignals([sig("handoff", "needs_rescope", { path: "lime/foo.py" })]),
    ).toEqual([
      { type: "needs_rescope", glyph: "◆", tone: "warn", text: "needs rescope · lime/foo.py" },
    ]);
  });

  test("input order is kept", () => {
    const out = phaseSignals([
      sig("budget_stop", "builder", { context_tokens: 240000, stop: 250000 }),
      sig("budget_warn", "builder", { context_tokens: 151000, warn: 150000 }),
      sig("timeout_kill", "builder", { elapsed_s: 60 }),
    ]);
    expect(out.map((s) => s.type)).toEqual([
      "budget_stop",
      "budget_warn",
      "timeout_kill",
    ]);
  });

  test("other event types and a planner handoff produce nothing", () => {
    expect(
      phaseSignals([
        sig("turn", "s1", { cost: 0.01 }),
        sig("tool_call", "s1", { tool: "bash" }),
        sig("log", "paths_touched", { paths: ["a"] }),
        sig("handoff", "planner", { prompt: "hi" }),
      ]),
    ).toEqual([]);
  });

  test("missing numbers give the bare label", () => {
    expect(phaseSignals([sig("budget_warn", "b", undefined)]).at(0)?.text).toBe(
      "budget warning",
    );
    expect(phaseSignals([sig("budget_stop", "b", undefined)]).at(0)?.text).toBe(
      "budget stop",
    );
    expect(phaseSignals([sig("timeout_kill", "b", undefined)]).at(0)?.text).toBe(
      "timeout kill",
    );
    expect(phaseSignals([sig("crash_detected", "b", undefined)]).at(0)?.text).toBe(
      "crash",
    );
  });

  test("a 200-char continuation error is cut to 90 chars plus an ellipsis", () => {
    const text = phaseSignals([
      sig("continuation", "b", { continuation: 2, error: "x".repeat(200) }),
    ]).at(0)?.text;
    expect(text?.endsWith("…")).toBe(true);
    expect(text?.length).toBe(91);
  });
});

// ── phaseError ───────────────────────────────────────────────────────────────

describe("phaseError", () => {
  test("fail with whitespace-wrapped text gives the trimmed error", () => {
    expect(phaseError(phase({ status: "fail", error: "  boom\n" }))).toBe("boom");
  });

  test("fail with blank text is null", () => {
    expect(phaseError(phase({ status: "fail", error: "   " }))).toBeNull();
  });

  test("success never carries an error", () => {
    expect(phaseError(phase({ status: "success", error: "x" }))).toBeNull();
  });

  test("fail with a null error is null", () => {
    expect(phaseError(phase({ status: "fail", error: null }))).toBeNull();
  });

  test("runError still returns the first failed phase's trimmed error", () => {
    expect(
      runError(
        [
          phase({ status: "fail", error: "  first\n" }),
          phase({ status: "fail", error: "second" }),
        ],
        [],
      ),
    ).toBe("first");
  });
});

// ── phaseCost ────────────────────────────────────────────────────────────────

describe("phaseCost", () => {
  test("openrouter turns fold into a cash figure", () => {
    const out = phaseCost(
      [session("s1", "p1", "openrouter/anthropic/claude-3.5")],
      [turn("s1", 0.03), turn("s1", 0.02)],
    );
    expect(out?.text).toBe("$0.05 · cash");
  });

  test("a subscription-only phase reads subscription with no dollar sign", () => {
    const out = phaseCost(
      [session("s1", "p1", "claude-opus-4")],
      [turn("s1", 0.4)],
    );
    expect(out?.text).toBe("subscription");
    expect(out?.text).not.toContain("$");
  });

  test("no Sessions and no turns is null", () => {
    expect(phaseCost([], [])).toBeNull();
  });

  test("a cash Session with no turns is $0.00 · cash", () => {
    const out = phaseCost([session("s1", "p1", "openrouter/x")], []);
    expect(out?.text).toBe("$0.00 · cash");
  });

  test("a cash and a subscription Session read cash +sub", () => {
    const out = phaseCost(
      [
        session("s1", "p1", "openrouter/x"),
        session("s2", "p1", "claude-opus-4"),
      ],
      [turn("s1", 0.05)],
    );
    expect(out?.text).toBe("$0.05 · cash +sub");
  });
});

// ── cardDetail fixture ────────────────────────────────────────────────────────

const RUN_ID = "r1";
const NOW = 1_700_000_000_000;

function feedEvent(seq: number, over: Partial<FeedEvent>): FeedEvent {
  return {
    seq,
    type: null,
    name: null,
    phase_id: null,
    parent_seq: null,
    payload: null,
    tokens: null,
    started_at: null,
    ...over,
  };
}

const RUN: Run = {
  run_id: RUN_ID,
  project: "proj",
  workflow: null,
  request: null,
  status: "running",
  engineer: null,
  started_at: null,
  ended_at: null,
  total_tokens: null,
  total_cost: null,
  archived: null,
  ticket: null,
  branch: null,
  worktree: null,
  port: null,
  db_name: null,
  profile: null,
  kind: null,
};

function feedRun(over: Partial<FeedRun> = {}): FeedRun {
  return {
    run: RUN,
    usage: { read: 0, written: 0 },
    phases: [],
    sessions: [],
    envelopes: [],
    gates: [],
    live: false,
    ...over,
  };
}

const BASE_PHASES: Phase[] = [
  phase({ phase_id: "p0", name: "engineer_1", kind: "engineer", owner: "eng", status: "success" }),
  phase({ phase_id: "p1", name: "plan", kind: "agent", owner: "planner", status: "success" }),
  phase({
    phase_id: "p2",
    name: "build_1_retry1",
    kind: "agent",
    owner: "builder",
    status: "fail",
    attempt: 2,
    error: "  boom\n",
  }),
  phase({ phase_id: "p3", name: "verify_1", kind: "code", owner: "runner", status: "success" }),
];

function env(
  id: number,
  phaseId: string,
  attempt: number,
  valid: number,
  payload: unknown,
  outputType = "BuildResult",
): Envelope {
  return {
    envelope_id: id,
    run_id: RUN_ID,
    phase_id: phaseId,
    agent: null,
    output_type: outputType,
    payload_json: payload === undefined ? null : JSON.stringify(payload),
    valid,
    attempt,
    created_at: null,
  };
}

function gate(
  id: number,
  phaseId: string,
  attempt: number,
  passed: number,
  violations: string | null,
  checks: unknown[] | null = null,
): GateResult {
  return {
    id,
    run_id: RUN_ID,
    phase_id: phaseId,
    attempt,
    gate: "diff",
    passed,
    violations_json: violations,
    checks_json: checks === null ? null : JSON.stringify(checks),
    created_at: null,
  };
}

function detailOf(
  run: FeedRun,
  events: FeedEvent[],
  phaseId: string | null,
  now = NOW,
): CardDetailView | null {
  return cardDetail(run, feedRows(RUN_ID, events), phaseId, now);
}

function factOf(view: CardDetailView, key: string): DetailFact | undefined {
  return view.facts.find((f) => f.key === key);
}

// ── cardDetail ────────────────────────────────────────────────────────────────

describe("cardDetail", () => {
  test("1. an unknown or null phase id is null", () => {
    const run = feedRun({ phases: BASE_PHASES });
    expect(detailOf(run, [], "nope")).toBeNull();
    expect(detailOf(run, [], null)).toBeNull();
  });

  test("2. status word, tone, attempt and multi", () => {
    const run = feedRun({ phases: BASE_PHASES });
    const p2 = detailOf(run, [], "p2")!;
    expect(p2.word).toBe("failed");
    expect(p2.tone).toBe("bad");
    expect(p2.attempt).toBe(2);
    expect(p2.multi).toBe(true);
    const p1 = detailOf(run, [], "p1")!;
    expect(p1.word).toBe("success");
    expect(p1.attempt).toBe(1);
    expect(p1.multi).toBe(false);
  });

  test("3a. context digits fill toward the Session budget_warn", () => {
    const events = [feedEvent(1, { type: "turn", name: "s2", phase_id: "p2", tokens: 61300 })];
    const sess = { ...session("s2", "p2", "openrouter/deepseek/x"), budget_warn: 150000 };
    const run = feedRun({ phases: BASE_PHASES, sessions: [sess] });
    const p2 = detailOf(run, events, "p2")!;
    expect(factOf(p2, "context")?.text).toBe("61k / 150k");
    expect(p2.context?.pct).toBeCloseTo(40.9, 1);
  });

  test("3b. the cap is never hardcoded 150k", () => {
    const events = [feedEvent(1, { type: "turn", name: "s2", phase_id: "p2", tokens: 61300 })];
    const sess = { ...session("s2", "p2", "openrouter/deepseek/x"), budget_warn: 200000 };
    const run = feedRun({ phases: BASE_PHASES, sessions: [sess] });
    expect(factOf(detailOf(run, events, "p2")!, "context")?.text).toBe("61k / 200k");
  });

  test("3c. with no budget_warn the digits stand alone", () => {
    const events = [feedEvent(1, { type: "turn", name: "s2", phase_id: "p2", tokens: 61300 })];
    const run = feedRun({
      phases: BASE_PHASES,
      sessions: [session("s2", "p2", "openrouter/deepseek/x")],
    });
    expect(factOf(detailOf(run, events, "p2")!, "context")?.text).toBe("61k");
  });

  test("3d. no turn tokens and no context_tokens hides the context fact", () => {
    const events = [feedEvent(1, { type: "turn", name: "s2", phase_id: "p2", tokens: null })];
    const run = feedRun({
      phases: BASE_PHASES,
      sessions: [session("s2", "p2", "openrouter/deepseek/x")],
    });
    expect(factOf(detailOf(run, events, "p2")!, "context")).toBeUndefined();
  });

  test("3e. a code phase has no context fact", () => {
    expect(factOf(detailOf(feedRun({ phases: BASE_PHASES }), [], "p3")!, "context")).toBeUndefined();
  });

  test("4a. cash turns fold into a phase cost fact", () => {
    const events = [
      feedEvent(1, { type: "turn", name: "s2", phase_id: "p2", payload: { cost: 0.03 } }),
      feedEvent(2, { type: "turn", name: "s2", phase_id: "p2", payload: { cost: 0.02 } }),
    ];
    const run = feedRun({
      phases: BASE_PHASES,
      sessions: [session("s2", "p2", "openrouter/deepseek/x")],
    });
    expect(factOf(detailOf(run, events, "p2")!, "cost")?.text).toBe("$0.05 · cash");
  });

  test("4b. a subscription phase reads subscription with no dollar sign", () => {
    const events = [feedEvent(1, { type: "turn", name: "s1", phase_id: "p1", payload: { cost: 0.4 } })];
    const run = feedRun({ phases: BASE_PHASES, sessions: [session("s1", "p1", "claude-opus-5-5")] });
    const text = factOf(detailOf(run, events, "p1")!, "cost")?.text;
    expect(text).toBe("subscription");
    expect(text).not.toContain("$");
  });

  test("4c. a code phase has no cost fact", () => {
    expect(factOf(detailOf(feedRun({ phases: BASE_PHASES }), [], "p3")!, "cost")).toBeUndefined();
  });

  test("5a. the latest valid envelope carries the outcome", () => {
    const run = feedRun({
      phases: BASE_PHASES,
      envelopes: [
        env(1, "p1", 1, 1, {
          status: "shipped",
          summary: "  did it  ",
          notes_for_next_agent: "  keep going\n",
        }),
        env(2, "p1", 2, 0, { status: "bad" }),
      ],
    });
    const out = phaseOutcome(run.envelopes, "p1")!;
    expect(out.valid).toBe(true);
    expect(out.type).toBe("BuildResult");
    expect(out.summary).toBe("did it");
    expect(out.status).toBe("shipped");
    expect(out.notes).toBe("keep going");
  });

  test("5d. a malformed payload_json yields null fields without throwing", () => {
    const e = env(1, "p1", 1, 1, undefined);
    e.payload_json = "{not json";
    const out = phaseOutcome([e], "p1")!;
    expect(out.summary).toBeNull();
    expect(out.status).toBeNull();
    expect(out.notes).toBeNull();
    expect(out.artifacts).toEqual([]);
  });

  test("5e. a phase with no envelopes has no outcome", () => {
    expect(phaseOutcome([], "p1")).toBeNull();
  });

  test("6a. paths are deduped across paths_touched events in first-seen order", () => {
    const events = [
      feedEvent(1, { type: "log", name: "paths_touched", phase_id: "p2", payload: { paths: ["a.ts", "b.ts"] } }),
      feedEvent(2, { type: "log", name: "paths_touched", phase_id: "p2", payload: { paths: ["b.ts", "c.ts"] } }),
    ];
    const p2 = detailOf(feedRun({ phases: BASE_PHASES }), events, "p2")!;
    expect(p2.touched?.paths).toEqual(["a.ts", "b.ts", "c.ts"]);
    expect(p2.touched?.label).toBe("3 files touched");
  });

  test("6b. diffFact skips a replayed verify", () => {
    const rows = [
      ev({ type: "log", name: "verify_1", payload_json: JSON.stringify({ total_added: 90, max_added_lines: 1500 }) }),
      ev({ type: "log", name: "verify_2_retry0", payload_json: JSON.stringify({ total_added: 1380, max_added_lines: 1500 }) }),
      ev({ type: "log", name: "verify_2", payload_json: JSON.stringify({ replayed: true }) }),
      ev({ type: "log", name: "paths_touched", payload_json: JSON.stringify({ total_added: 5 }) }),
    ];
    const d = diffFact(rows)!;
    expect(d.text).toBe("+1,380 of 1,500 lines");
    expect(d.over).toBe(false);
  });

  test("6c. an over-cap measurement is flagged", () => {
    const rows = [ev({ type: "log", name: "verify_1", payload_json: JSON.stringify({ total_added: 1600, max_added_lines: 1500 }) })];
    expect(diffFact(rows)?.over).toBe(true);
  });

  test("6d. a zero cap drops the comparison", () => {
    const rows = [ev({ type: "log", name: "verify_1", payload_json: JSON.stringify({ total_added: 90, max_added_lines: 0 }) })];
    expect(diffFact(rows)?.text).toBe("+90 lines");
  });

  test("6e. non-integer measurements are null", () => {
    const mk = (total: unknown) => [
      ev({ type: "log", name: "verify_1", payload_json: JSON.stringify({ total_added: total, max_added_lines: 1500 }) }),
    ];
    expect(diffFact(mk("12"))).toBeNull();
    expect(diffFact(mk(true))).toBeNull();
    expect(diffFact(mk(1.5))).toBeNull();
  });

  test("6f. with no measurement the label stands alone", () => {
    const events = [
      feedEvent(1, { type: "log", name: "paths_touched", phase_id: "p2", payload: { paths: ["a.ts", "b.ts", "c.ts"] } }),
    ];
    const p2 = detailOf(feedRun({ phases: BASE_PHASES }), events, "p2")!;
    expect(p2.touched?.diff).toBeNull();
    expect(p2.touched?.label).toBe("3 files touched");
  });

  test("6g. one path is singular and none is null", () => {
    const one = [feedEvent(1, { type: "log", name: "paths_touched", phase_id: "p2", payload: { paths: ["a.ts"] } })];
    expect(detailOf(feedRun({ phases: BASE_PHASES }), one, "p2")?.touched?.label).toBe("1 file touched");
    expect(detailOf(feedRun({ phases: BASE_PHASES }), [], "p2")?.touched).toBeNull();
  });

  test("7a. the planner brief is the plan_provenance handoff prompt", () => {
    const events = [feedEvent(1, { type: "handoff", name: "plan_provenance", phase_id: "p1", payload: { prompt: "Ticket: 265 …" } })];
    expect(detailOf(feedRun({ phases: BASE_PHASES }), events, "p1")?.brief).toBe("Ticket: 265 …");
  });

  test("7b. a non-plan phase has no brief and a blank prompt is ignored", () => {
    expect(detailOf(feedRun({ phases: BASE_PHASES }), [], "p2")?.brief).toBeNull();
    const blank = [feedEvent(1, { type: "handoff", name: "plan_provenance", phase_id: "p1", payload: { prompt: "   " } })];
    expect(detailOf(feedRun({ phases: BASE_PHASES }), blank, "p1")?.brief).toBeNull();
  });

  test("7c. the engineer request is its own log prompt", () => {
    const events = [feedEvent(1, { type: "log", name: "engineer", phase_id: "p0", payload: { prompt: "do the thing" } })];
    const run = feedRun({ phases: BASE_PHASES });
    expect(detailOf(run, events, "p0")?.request).toBe("do the thing");
    expect(detailOf(run, events, "p1")?.request).toBeNull();
  });

  test("8a. the footer session id is the latest Session and the view carries its id", () => {
    const run = feedRun({
      phases: BASE_PHASES,
      sessions: [
        { ...session("s2a", "p2", "openrouter/deepseek/x"), continuation: 0 },
        { ...session("s2b", "p2", "openrouter/deepseek/x"), continuation: 1 },
      ],
    });
    const p2 = detailOf(run, [], "p2")!;
    expect(p2.sessionId).toBe("s2b");
    expect(p2.id).toBe("p2");
  });

  test("8b. an in-flight phase reads its session id from agent_start", () => {
    const events = [feedEvent(1, { type: "agent_start", name: "s9", phase_id: "p2", payload: { session_id: "s9" } })];
    expect(detailOf(feedRun({ phases: BASE_PHASES }), events, "p2")?.sessionId).toBe("s9");
  });

  test("8c. a code phase has no session id", () => {
    expect(detailOf(feedRun({ phases: BASE_PHASES }), [], "p3")?.sessionId).toBeNull();
  });

  test("9. tool-call args and results never reach the view", () => {
    const events = [
      feedEvent(1, { type: "turn", name: "s2", phase_id: "p2", payload: { cost: 0.01 } }),
      feedEvent(2, { type: "turn", name: "s2", phase_id: "p2", payload: { cost: 0.01 } }),
      feedEvent(3, { type: "tool_call", name: "s2", phase_id: "p2", payload: { tool: "bash", args: { command: "TOOLARG_MARKER" } } }),
      feedEvent(4, { type: "tool_result", name: "s2", phase_id: "p2", payload: { tail: "TAIL_MARKER" } }),
    ];
    const run = feedRun({
      phases: BASE_PHASES,
      sessions: [session("s2", "p2", "openrouter/deepseek/x")],
    });
    const view = detailOf(run, events, "p2")!;
    const json = JSON.stringify(view);
    expect(json).not.toContain("TOOLARG_MARKER");
    expect(json).not.toContain("TAIL_MARKER");
    expect(factOf(view, "work")?.text).toBe("2 turns · 1 tool call");
  });

  test("10. signals surface and the phase error is trimmed", () => {
    const events = [feedEvent(1, { type: "budget_warn", name: "s2", phase_id: "p2", payload: { context_tokens: 151000, warn: 150000 } })];
    const run = feedRun({ phases: BASE_PHASES });
    const p2 = detailOf(run, events, "p2")!;
    expect(p2.signals.some((s) => s.type === "budget_warn")).toBe(true);
    expect(p2.error).toBe("boom");
    expect(detailOf(run, [], "p1")?.error).toBeNull();
  });

  test("11a. gate rows are ordered by attempt and list their violations", () => {
    const run = feedRun({
      phases: BASE_PHASES,
      gates: [
        gate(1, "p2", 2, 1, "[]"),
        gate(2, "p2", 1, 0, JSON.stringify(["over cap"])),
      ],
    });
    const rows = phaseChecks(run.gates, run.envelopes, "p2");
    expect(rows.map((r) => r.attempt)).toEqual([1, 2]);
    expect(rows[0].ok).toBe(false);
    expect(rows[0].violations).toEqual(["over cap"]);
    expect(rows[1].ok).toBe(true);
    expect(rows[1].violations).toEqual([]);
  });

  test("11b. an invalid envelope row carries its raw text cut to 300 chars", () => {
    const e = env(1, "p2", 1, 0, undefined);
    e.payload_json = "x".repeat(400);
    const rows = phaseChecks([], [e], "p2");
    expect(rows[0].ok).toBe(false);
    expect(rows[0].violations).toEqual(["x".repeat(300)]);
  });

  test("11c. a malformed violations_json falls back to the raw string", () => {
    const run = feedRun({ phases: BASE_PHASES, gates: [gate(1, "p2", 1, 0, "not-json")] });
    expect(phaseChecks(run.gates, [], "p2")[0].violations).toEqual(["not-json"]);
  });

  test("11d. a passed gate carries its checks and a check count", () => {
    const checks = [
      { item: "lime/a.py", ok: true, note: "exists, 12B" },
      { item: "lime/b.py", ok: true, note: "exists, 3B" },
    ];
    const run = feedRun({ phases: BASE_PHASES, gates: [gate(1, "p2", 1, 1, "[]", checks)] });
    const row = phaseChecks(run.gates, [], "p2")[0];
    expect(row.ok).toBe(true);
    expect(row.checks).toEqual(checks);
    expect(row.count).toBe("2 checks");
  });

  test("11e. a failed gate sums its passing evidence as other checks", () => {
    const checks = [
      { item: "lime/a.py", ok: true, note: "exists" },
      { item: "lime/b.py", ok: false, note: "missing" },
    ];
    const run = feedRun({
      phases: BASE_PHASES,
      gates: [gate(1, "p2", 1, 0, JSON.stringify(["lime/b.py missing"]), checks)],
    });
    const row = phaseChecks(run.gates, [], "p2")[0];
    expect(row.ok).toBe(false);
    expect(row.violations).toEqual(["lime/b.py missing"]);
    expect(row.count).toBe("1 other check passed");
  });

  test("11f. a gate with no checks has no count and an empty list", () => {
    const run = feedRun({ phases: BASE_PHASES, gates: [gate(1, "p2", 1, 1, "[]")] });
    const row = phaseChecks(run.gates, [], "p2")[0];
    expect(row.checks).toEqual([]);
    expect(row.count).toBeNull();
  });

  test("11g. gateChecks tolerates malformed and non-array payloads", () => {
    expect(gateChecks(null)).toEqual([]);
    expect(gateChecks("not-json")).toEqual([]);
    expect(gateChecks(JSON.stringify({ item: "x" }))).toEqual([]);
    expect(gateChecks(JSON.stringify([{ item: "x" }]))).toEqual([
      { item: "x", ok: false, note: "" },
    ]);
  });

  test("11h. an envelope row carries no checks and no count", () => {
    const rows = phaseChecks([], [env(1, "p2", 1, 1, { status: "ok" })], "p2");
    expect(rows[0].checks).toEqual([]);
    expect(rows[0].count).toBeNull();
  });

  test("11i. repoPath keeps repo-relative paths and rejects the rest", () => {
    expect(repoPath("plan.md")).toBe("plan.md");
    expect(repoPath("  ./lime/a.py ")).toBe("lime/a.py");
    expect(repoPath("/etc/passwd")).toBeNull();
    expect(repoPath("~/x.md")).toBeNull();
    expect(repoPath("C:\\x.md")).toBeNull();
    expect(repoPath("https://example.com/x.md")).toBeNull();
    expect(repoPath("../outside.md")).toBeNull();
    expect(repoPath("   ")).toBeNull();
  });

  test("11j. an envelope artifact is openable only when it is in the repo", () => {
    const out = phaseOutcome(
      [env(1, "p1", 1, 1, { artifacts: ["plan.md", "/tmp/abs.md"] })],
      "p1",
    )!;
    expect(out.artifacts).toEqual([
      { path: "plan.md", open: "plan.md" },
      { path: "/tmp/abs.md", open: null },
    ]);
  });

  test("12a. a finished phase shows clock → clock · duration", () => {
    const s = new Date(NOW - 3_600_000).toISOString();
    const e = new Date(NOW - 60_000).toISOString();
    const phases: Phase[] = BASE_PHASES.map((p) =>
      p.phase_id === "p3" ? { ...p, started_at: s, ended_at: e, status: "success" } : p,
    );
    const p3 = detailOf(feedRun({ phases }), [], "p3")!;
    expect(factOf(p3, "time")?.text).toBe(
      `${fmtClock(s)} → ${fmtClock(e)} · ${fmtDuration(s, e, NOW)}`,
    );
  });

  test("12b. a running phase counts since its start", () => {
    const s = new Date(NOW - 60_000).toISOString();
    const phases: Phase[] = BASE_PHASES.map((p) =>
      p.phase_id === "p1" ? { ...p, started_at: s, status: "running" } : p,
    );
    const text = factOf(detailOf(feedRun({ phases }), [], "p1")!, "time")?.text;
    expect(text?.startsWith("since ")).toBe(true);
  });

  test("13a. a running agent phase shows its live line", () => {
    const phases: Phase[] = BASE_PHASES.map((p) =>
      p.phase_id === "p1" ? { ...p, status: "running" } : p,
    );
    const events = [
      feedEvent(1, {
        type: "tool_call",
        name: "Bash",
        phase_id: "p1",
        payload: { tool_call_id: "t1", tool: "Bash" },
      }),
    ];
    expect(detailOf(feedRun({ phases }), events, "p1")!.live).toBe("Bash…");
  });

  test("13b. a running agent phase with no open call is thinking", () => {
    const phases: Phase[] = BASE_PHASES.map((p) =>
      p.phase_id === "p1" ? { ...p, status: "running" } : p,
    );
    expect(detailOf(feedRun({ phases }), [], "p1")!.live).toBe("Thinking…");
  });

  test("13c. a finished phase and a code phase have no live line", () => {
    const finished = detailOf(feedRun({ phases: BASE_PHASES }), [], "p1")!;
    expect(finished.live).toBeNull();
    const code: Phase[] = BASE_PHASES.map((p) =>
      p.phase_id === "p3" ? { ...p, status: "running" } : p,
    );
    expect(detailOf(feedRun({ phases: code }), [], "p3")!.live).toBeNull();
  });
});

// ── CardDetail.vue source guards ─────────────────────────────────────────────

function styleBlocks(src: string): { scoped: boolean; css: string }[] {
  const out: { scoped: boolean; css: string }[] = [];
  const re = /<style(\s[^>]*)?>([\s\S]*?)<\/style>/g;
  for (const m of src.matchAll(re)) {
    out.push({ scoped: /\bscoped\b/.test(m[1] ?? ""), css: m[2] });
  }
  return out;
}

function cssRules(css: string): { selector: string; body: string }[] {
  const out: { selector: string; body: string }[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  for (const m of css.matchAll(re)) {
    out.push({ selector: m[1].trim(), body: m[2] });
  }
  return out;
}

const COMPONENTS = join(import.meta.dir, "../components");
const readComponent = (name: string) => readFileSync(join(COMPONENTS, name), "utf8");

function rule(blocks: { scoped?: boolean; css: string }[], selector: string) {
  for (const b of blocks) {
    const found = cssRules(b.css).find((r) => r.selector === selector);
    if (found) return found;
  }
  return undefined;
}

describe("CardDetail.vue", () => {
  const src = readComponent("CardDetail.vue");
  const blocks = styleBlocks(src);

  const openTag = (cls: string) =>
    src.match(new RegExp(`<details[^>]*class="[^"]*${cls}[^"]*"[^>]*>`))?.[0] ?? "";

  const blockOf = (cls: string) =>
    src.match(
      new RegExp(`<details[^>]*class="[^"]*${cls}[^"]*"[^>]*>[\\s\\S]*?<\\/details>`),
    )?.[0] ?? "";

  test("every style block is scoped and uncapped", () => {
    expect(blocks.length).toBeGreaterThan(0);
    for (const b of blocks) {
      expect(b.scoped).toBe(true);
      expect(/max-width\s*:\s*(?!none\b)/.test(b.css)).toBe(false);
    }
  });

  test("reads only its props", () => {
    expect(src).toContain("defineProps");
    expect(src).toContain("cardDetail(");
    expect(src).toContain("feedRows(");
    expect(src).not.toContain("fetch(");
    expect(src).not.toContain("lib/api");
    expect(src).not.toContain("EventSource");
    expect(src).not.toContain("lib/router");
  });

  test("no tool-call rows", () => {
    for (const word of ["tool_call", "toolCall", "argsSummary", "parseToolCall", "Tool calls"]) {
      expect(src).not.toContain(word);
    }
  });

  test("a long summary clamps to five rendered lines with a measured More", () => {
    expect(rule(blocks, ".cd-summary.clamp")?.body).toContain("-webkit-line-clamp: 5");
    expect(src).toContain('ref="summaryEl"');
    expect(src).toContain(':class="{ clamp: !more }"');
    expect(src).toContain("el.scrollHeight - el.clientHeight");
    expect(src).toContain('@click="more = !more"');
    expect(src).toContain("'More'");
  });

  test("notes, brief and touched start collapsed; request starts open", () => {
    for (const cls of ["cd-notes", "cd-brief", "cd-touched"]) {
      const tag = openTag(cls);
      expect(tag).not.toBe("");
      expect(tag).not.toContain(" open");
    }
    expect(openTag("cd-request")).toContain(" open");
  });

  test("the notes text sits inside the cd-notes block", () => {
    expect(blockOf("cd-notes")).toContain("Notes for the next phase");
  });

  test("the footer carries the phase id, session id and copy handler", () => {
    const start = src.indexOf('<footer class="cd-foot"');
    const foot = src.slice(start, src.indexOf("</footer>", start));
    expect(foot).toContain("detail.id");
    expect(foot).toContain("detail.sessionId");
    expect(foot).toContain('@click="copySession"');
    expect(src).toContain("navigator.clipboard.writeText");
  });

  test("the cd-touched block shows the label and diff text", () => {
    const block = blockOf("cd-touched");
    expect(block).toContain("detail.touched.label");
    expect(block).toContain("detail.touched.diff.text");
  });

  test("the state line shows the word and attempt", () => {
    expect(src).toContain("detail.word");
    expect(src).toContain("attempt {{ detail.attempt }}");
  });

  test("the More reset watches phase and summary as separate sources", () => {
    // A single getter returning an array hands back a fresh array on every
    // recompute, and the 1 s clock recomputes `detail` every tick even when
    // neither value changed, which would reset More every second. Watching
    // separate getters compares each source by value instead.
    expect(src).not.toContain("() => [props.phaseId");
    expect(src).toContain("[() => props.phaseId, () => detail.value?.outcome?.summary]");
  });

  test("the clock is cleaned up", () => {
    expect(src).toContain("setInterval(");
    expect(src).toContain("clearInterval(");
    expect(src).toContain("onUnmounted");
  });

  test("the live line sits in the body", () => {
    expect(src).toContain("detail.live");
  });

  test("a passed gate is a collapsed row with its check count", () => {
    const checks = src.slice(src.indexOf('class="cd-sec cd-checks"'));
    expect(checks).toContain('<details');
    expect(checks).toContain(':open="!c.ok"');
    expect(checks).toContain("c.count");
    expect(checks).toContain("c.checks");
    expect(checks).toContain("k.item");
    expect(checks).toContain("k.note");
  });

  test("a failed gate opens with its violations", () => {
    const checks = src.slice(src.indexOf('class="cd-sec cd-checks"'));
    expect(checks).toContain("c.violations");
    expect(checks).toContain(':open="!c.ok"');
  });

  test("a repo artifact is a chip that opens, an outside one is text", () => {
    expect(src).toContain('defineEmits<{ open: [path: string] }>');
    expect(src).toContain('@click="openArtifact(a)"');
    expect(src).toContain('emit("open", a.open)');
    expect(src).toContain("a.open");
    expect(src).toContain("a.path");
  });
});
