import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import type {
  EventRow,
  FeedEvent,
  Phase,
  Run,
  RunDetail,
  Session,
} from "./types";
import { cashDetail, cashWord } from "./cash";
import { runLineTint, type LineTint } from "./status";
import {
  failureLine,
  feedRows,
  lineTicket,
  lineWord,
  runElapsed,
  runInfo,
  runLine,
} from "./runLine";

// ── Builders ────────────────────────────────────────────────────────────────

function run(over: Partial<Run>): Run {
  return {
    run_id: "83b4e2d1",
    project: "limefactory",
    workflow: null,
    request: null,
    status: null,
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
    ...over,
  };
}

function session(id: string, model: string | null): Session {
  return {
    session_id: id,
    run_id: "83b4e2d1",
    phase_id: "p1",
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
    run_id: "83b4e2d1",
    seq: null,
    name: null,
    kind: null,
    owner: null,
    description: null,
    status: null,
    attempt: null,
    retries: null,
    error: null,
    started_at: null,
    ended_at: null,
    ...over,
  };
}

function detail(
  r: Run,
  over: { sessions?: Session[]; phases?: Phase[] } = {},
): RunDetail {
  return {
    run: r,
    usage: { read: 0, written: 0 },
    phases: over.phases ?? [],
    sessions: over.sessions ?? [],
  };
}

function fe(
  seq: number,
  type: FeedEvent["type"],
  name: string | null,
  payload: unknown,
  over: Partial<FeedEvent> = {},
): FeedEvent {
  return {
    seq,
    type,
    name,
    phase_id: null,
    parent_seq: null,
    payload,
    tokens: null,
    started_at: null,
    ...over,
  };
}

const NOW = Date.parse("2026-01-01T12:00:00Z");

// ── Tests ───────────────────────────────────────────────────────────────────

test("reads the spec's line", () => {
  const started = new Date(NOW - (38 * 60 + 6) * 1000).toISOString();
  const r = run({
    status: "running",
    ticket: "specs/tickets/214-notch.md",
    started_at: started,
  });
  const events = feedRows(r.run_id, [
    fe(1, "turn", "s1", { cost: 0.05 }),
    fe(2, "budget_warn", "warn", {}),
  ]);
  const d = detail(r, { sessions: [session("s1", "openrouter/deepseek/x")] });
  expect(runLine(d, events, NOW).text).toBe(
    "● Running · 214 · 38:06 · $0.05 · ▲1",
  );
});

test("▲N only when N > 0", () => {
  const r = run({
    status: "running",
    ticket: "specs/tickets/214-notch.md",
    started_at: new Date(NOW - 1000).toISOString(),
  });
  const d = detail(r, { sessions: [session("s1", "openrouter/deepseek/x")] });
  const none = feedRows(r.run_id, [fe(1, "turn", "s1", { cost: 0.05 })]);
  const line = runLine(d, none, NOW);
  expect(line.text).not.toContain("▲");
  expect(line.label.endsWith("$0.05")).toBe(true);

  const two = feedRows(r.run_id, [
    fe(1, "turn", "s1", { cost: 0.05 }),
    fe(2, "budget_warn", "w", {}),
    fe(3, "budget_warn", "w", {}),
  ]);
  expect(runLine(d, two, NOW).text.endsWith("▲2")).toBe(true);
});

test("cash counts only openrouter/* spend", () => {
  const r = run({ status: "running" });
  const d = detail(r, {
    sessions: [
      session("s1", "openrouter/deepseek/x"),
      session("s2", "claude-opus-x"),
      session("s3", "openai-codex/gpt"),
    ],
  });
  const events = feedRows(r.run_id, [
    fe(1, "turn", "s1", { cost: 0.05 }),
    fe(2, "turn", "s2", { cost: 0.29 }),
    fe(3, "turn", "s3", { cost: 0.1 }),
  ]);
  const line = runLine(d, events, NOW);
  expect(line.label.split(" · ")[3]).toBe("$0.05");

  const d2 = detail(r, { sessions: [session("s1", "OpenRouter/x")] });
  const e2 = feedRows(r.run_id, [fe(1, "turn", "s1", { cost: 0.05 })]);
  expect(runLine(d2, e2, NOW).label.split(" · ")[3]).toBe("$0.05");
});

test("a Run with no cash shows subscription, never $0.00", () => {
  const cases: Array<[RunDetail, EventRow[]]> = [
    [
      detail(run({ status: "success" }), {
        sessions: [session("s1", "claude-opus-x")],
      }),
      feedRows("83b4e2d1", [fe(1, "turn", "s1", { cost: 0.29 })]),
    ],
    [detail(run({ status: "success" })), []],
    [
      detail(run({ status: "running" }), {
        sessions: [session("s1", "openrouter/x")],
      }),
      feedRows("83b4e2d1", [fe(1, "turn", "s1", { cost: 0 })]),
    ],
  ];
  for (const [d, events] of cases) {
    const line = runLine(d, events, NOW);
    expect(line.label).toContain("subscription");
    expect(line.label).not.toContain("$");
  }

  expect(cashWord({ cash: 0 })).toBe("subscription");
  const cd = cashDetail({ cash: 0, sub: true }, 0.29, true);
  expect(cd.text).toBe("subscription");
  expect(cd.title).not.toContain("$");
});

test("cashDetail with cash", () => {
  expect(cashDetail({ cash: 0.05, sub: true }, 0.31, true)).toEqual({
    text: "$0.05 cash +sub so far",
    title: "cash $0.05 · booked $0.31",
  });
  expect(cashDetail({ cash: 0.05, sub: false }, null, false).text).toBe(
    "$0.05 cash",
  );
});

const TINTS: Array<[string | null, LineTint, LineTint]> = [
  ["running", "quiet", "warn"],
  ["success", "quiet", "warn"],
  ["fail", "bad", "bad"],
  ["budget_exhausted", "bad", "bad"],
  ["deployed", "quiet", "warn"],
  ["discarded", "quiet", "warn"],
  ["needs_rescope", "warn", "warn"],
  ["mystery-status", "quiet", "warn"],
  [null, "quiet", "warn"],
];

describe("tint over every Run status the trace writes", () => {
  for (const [status, at0, at1] of TINTS) {
    const rows: Array<[number, LineTint]> = [
      [0, at0],
      [1, at1],
    ];
    for (const [warns, expected] of rows) {
      test(`${status ?? "null"} + ${warns} warn(s) → ${expected}`, () => {
        expect(runLineTint(status, warns)).toBe(expected);
        const r = run({ status: status as Run["status"] });
        const events = feedRows(
          r.run_id,
          Array.from({ length: warns }, (_, i) =>
            fe(i + 1, "budget_warn", "w", {}),
          ),
        );
        expect(runLine(detail(r), events, NOW).tint).toBe(expected);
      });
    }
  }

  test("every trace status is a row in the table", () => {
    const src = readFileSync(
      join(import.meta.dir, "../../../lime/tracer.py"),
      "utf8",
    );
    const keys = new Set<string>();
    const block = src.split("RUN_TRANSITIONS = {", 2)[1].split("\n}")[0];
    for (const m of block.matchAll(/^\s+"(\w+)":/gm)) keys.add(m[1]);
    const legacy = src.match(/LEGACY_RUN_TRANSITIONS = \{([^}]*)\}/);
    if (legacy) {
      for (const m of legacy[1].matchAll(/"(\w+)":/g)) keys.add(m[1]);
    }
    expect(keys.size).toBeGreaterThan(0);
    const rows = new Set(TINTS.map(([s]) => s));
    for (const key of keys) {
      expect(rows.has(key)).toBe(true);
    }
  });
});

test("the word", () => {
  expect(lineWord("fail")).toBe("Failed");
  expect(lineWord("needs_rescope")).toBe("Needs rescope");
  expect(lineWord("mystery-status")).toBe("Mystery-status");
  expect(lineWord(null)).toBe("Unknown");
});

test("elapsed", () => {
  const open = run({
    started_at: new Date(NOW - (3600 + 120 + 3) * 1000).toISOString(),
  });
  expect(runElapsed(open, NOW)).toBe("1:02:03");

  const started = "2026-01-01T10:00:00Z";
  const ended = run({
    started_at: started,
    ended_at: new Date(Date.parse(started) + 42 * 60 * 1000).toISOString(),
  });
  expect(runElapsed(ended, NOW)).toBe("42m");
  expect(
    runLine(detail(ended), feedRows(ended.run_id, []), NOW).label.split(
      " · ",
    )[2],
  ).toBe("42m");

  expect(runElapsed(run({}), NOW)).toBe("—");
});

test("ticket segment", () => {
  expect(lineTicket(run({ ticket: "specs/tickets/214-notch.md" }))).toBe("214");
  expect(lineTicket(run({ ticket: "266-run-line-and-run-tab" }))).toBe("266");
  expect(lineTicket(run({ ticket: null, run_id: "abc123" }))).toBe("abc123");
  expect(lineTicket(run({ ticket: "notes", run_id: "abc123" }))).toBe("abc123");
});

test("feedRows", () => {
  const rows = feedRows("r-1", [
    fe(7, "turn", "s1", { cost: 0.05 }, { parent_seq: 3, phase_id: "p1" }),
  ]);
  expect(rows[0].event_id).toBe(7);
  expect(rows[0].rowid).toBe(7);
  expect(rows[0].run_id).toBe("r-1");
  expect(rows[0].parent_id).toBe(3);
  expect(rows[0].phase_id).toBe("p1");
  expect(JSON.parse(rows[0].payload_json as string)).toEqual({ cost: 0.05 });
  expect(rows[0].ended_at).toBeNull();

  const nullPayload = feedRows("r-1", [fe(1, "turn", null, null)]);
  expect(nullPayload[0].payload_json).toBeNull();
});

test("feedRows carries the rescope decision link", () => {
  const r = run({ status: "needs_rescope", worktree: "/wt" });
  const events = feedRows(r.run_id, [
    fe(7, "handoff", "needs_rescope", { path: "handoff.md" }),
    fe(8, "log", "rescope_recovery_decision", { decision: "rerun" }, {
      parent_seq: 7,
    }),
  ]);
  expect(runInfo(detail(r), events, NOW).notice?.decision).toBe("rerun");
});

test("runInfo facts", () => {
  const long =
    "lime/some-very-long-branch-name-that-should-never-be-truncated-anywhere";
  const full = run({
    status: "running",
    project: "limefactory",
    workflow: "feature",
    started_at: new Date(NOW - 1000).toISOString(),
    total_cost: 0.05,
    branch: long,
    port: 4612,
    db_name: "lime_4612",
    profile: "default",
    worktree: "/tmp/wt",
    engineer: "alice",
    total_tokens: 61000,
  });
  const info = runInfo(detail(full), feedRows(full.run_id, []), NOW);
  expect(info.facts.map((f) => f.key)).toEqual([
    "project",
    "workflow",
    "elapsed",
    "cost",
    "branch",
    "port",
    "db",
    "profile",
    "worktree",
    "engineer",
    "tokens",
  ]);
  expect(info.facts.find((f) => f.key === "port")?.text).toBe(":4612");
  expect(info.facts.find((f) => f.key === "branch")?.text).toBe(long);

  const bare = run({ project: "limefactory", started_at: full.started_at });
  expect(
    runInfo(detail(bare), feedRows(bare.run_id, []), NOW).facts.map(
      (f) => f.key,
    ),
  ).toEqual(["project", "elapsed", "cost"]);

  const two = feedRows(full.run_id, [
    fe(1, "budget_warn", "w", {}),
    fe(2, "budget_warn", "w", {}),
  ]);
  const info2 = runInfo(detail(full), two, NOW);
  const keys2 = info2.facts.map((f) => f.key);
  expect(keys2.indexOf("warns")).toBe(keys2.indexOf("elapsed") + 1);
  expect(info2.facts.find((f) => f.key === "warns")?.text).toBe("▲ 2 warns");
  expect(info2.facts.find((f) => f.key === "warns")?.title).toBeTruthy();

  const one = feedRows(full.run_id, [fe(1, "budget_warn", "w", {})]);
  expect(
    runInfo(detail(full), one, NOW).facts.find((f) => f.key === "warns")?.text,
  ).toBe("▲ 1 warn");
});

test("runInfo stop/kill/crash chips", () => {
  const r = run({ status: "fail" });
  const events = feedRows(r.run_id, [
    fe(1, "budget_warn", "w", {}),
    fe(2, "budget_stop", "b", {}),
    fe(3, "timeout_kill", "t", { elapsed_s: 30 }),
    fe(4, "crash_detected", "c", { signal: "SIGKILL", exit_code: 137 }),
  ]);
  const info = runInfo(detail(r), events, NOW);
  expect(info.signals.map((s) => s.type)).toEqual([
    "budget_stop",
    "timeout_kill",
    "crash_detected",
  ]);
  expect(info.signals.every((s) => s.tone === "bad")).toBe(true);
});

test("failureLine", () => {
  expect(
    failureLine(
      [
        phase({ phase_id: "p1", name: "build", status: "success" }),
        phase({ phase_id: "p2", name: "review", status: "fail", error: "boom" }),
      ],
      [],
    ),
  ).toEqual({ phaseId: "p2", text: "review failed · boom" });

  expect(
    failureLine([phase({ phase_id: "p1", status: "fail", error: "   " })], []),
  ).toBeNull();

  expect(
    failureLine([], feedRows("r", [fe(1, "error", null, { error: "died" })])),
  ).toEqual({ phaseId: null, text: "failed · died" });

  expect(failureLine([], [])).toBeNull();
});

test("notice outranks failure", () => {
  const phases = [
    phase({ phase_id: "p2", name: "review", status: "fail", error: "boom" }),
  ];
  const rescope = run({ status: "needs_rescope", worktree: "/wt" });
  const ev = feedRows(rescope.run_id, [
    fe(7, "handoff", "needs_rescope", { path: "h.md" }),
  ]);
  const info = runInfo(detail(rescope, { phases }), ev, NOW);
  expect(info.notice).not.toBeNull();
  expect(info.failure).toBeNull();

  const failed = run({ status: "fail" });
  const info2 = runInfo(
    detail(failed, { phases }),
    feedRows(failed.run_id, []),
    NOW,
  );
  expect(info2.notice).toBeNull();
  expect(info2.failure).not.toBeNull();
});

test("noticePhase names the phase the rescope notice points at", () => {
  const phases = [
    phase({ phase_id: "p3", name: "build_3" }),
    phase({ phase_id: "p4", name: null }),
  ];
  const rescope = run({ status: "needs_rescope", worktree: "/wt" });
  const ev = feedRows(rescope.run_id, [
    fe(7, "handoff", "needs_rescope", { path: "h.md" }, { phase_id: "p3" }),
  ]);
  const info = runInfo(detail(rescope, { phases }), ev, NOW);
  expect(info.noticePhase).toEqual({ id: "p3", name: "build_3" });

  // The last rescope event that names a phase wins, and a nameless phase
  // falls back to its id.
  const two = feedRows(rescope.run_id, [
    fe(7, "handoff", "needs_rescope", { path: "h.md" }, { phase_id: "p3" }),
    fe(8, "handoff", "needs_rescope", { path: "h2.md" }, { phase_id: "p4" }),
  ]);
  expect(runInfo(detail(rescope, { phases }), two, NOW).noticePhase).toEqual({
    id: "p4",
    name: "p4",
  });

  // No notice, no phase.
  const running = run({ status: "running" });
  expect(
    runInfo(detail(running, { phases }), feedRows(running.run_id, []), NOW)
      .noticePhase,
  ).toBeNull();
});

// ── Source guards ───────────────────────────────────────────────────────────

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
const readComponent = (name: string) =>
  readFileSync(join(COMPONENTS, name), "utf8");

function rule(blocks: { scoped?: boolean; css: string }[], selector: string) {
  for (const b of blocks) {
    const found = cssRules(b.css).find((r) => r.selector === selector);
    if (found) return found;
  }
  return undefined;
}

describe("RunLine.vue", () => {
  const src = readComponent("RunLine.vue");
  const blocks = styleBlocks(src);

  test("scoped style, no cap", () => {
    expect(blocks.length).toBeGreaterThan(0);
    for (const b of blocks) {
      expect(b.scoped).toBe(true);
      expect(b.css).not.toContain("max-width");
    }
  });

  test("clicking the line asks for the Run tab", () => {
    expect(src).toContain("defineEmits");
    expect(src).toContain("openRunTab");
    const m = src.match(/<button[^>]*@click="emit\('openRunTab'\)"/);
    expect(m).not.toBeNull();
    expect(m?.[0]).toContain('class="run-line"');
  });

  test("reads only its props", () => {
    expect(src).toContain("runLine(");
    expect(src).toContain("feedRows(");
    expect(src).not.toContain("fetch(");
    expect(src).not.toContain("lib/api");
    expect(src).not.toContain("EventSource");
    expect(src).not.toContain("lib/router");
  });

  test("never draws stop/kill/crash chips", () => {
    expect(src).not.toContain("chip");
    expect(src).not.toContain("runSignals");
  });

  test("tints", () => {
    expect(src).toContain(':class="line.tint"');
    expect(rule(blocks, ".run-line.bad")?.body).toContain("var(--bad)");
    expect(rule(blocks, ".run-line.warn")?.body).toContain("var(--warn)");
  });

  test("clock is cleaned up", () => {
    expect(src).toContain("setInterval(");
    expect(src).toContain("clearInterval(");
    expect(src).toContain("onUnmounted");
  });
});

describe("RunInfo.vue", () => {
  const src = readComponent("RunInfo.vue");
  const blocks = styleBlocks(src);

  test("scoped style, no cap but none", () => {
    expect(blocks.length).toBeGreaterThan(0);
    for (const b of blocks) {
      expect(b.scoped).toBe(true);
      expect(/max-width\s*:\s*(?!none\b)/.test(b.css)).toBe(false);
    }
  });

  test("the .chip rule wraps and is uncapped", () => {
    const chip = blocks
      .filter((b) => b.scoped)
      .flatMap((b) => cssRules(b.css))
      .find((r) => r.selector === ".chip");
    expect(chip).toBeDefined();
    expect(chip?.body).toContain("white-space: normal");
    expect(chip?.body).toContain("overflow-wrap: anywhere");
  });

  test("holds every piece", () => {
    expect(src).toContain("runInfo(");
    expect(src).toContain("feedRows(");
    expect(src).toContain("<StatusChip");
    expect(src).toContain("info.facts");
    expect(src).toContain('class="chip signal"');
    expect(src).toContain("info.signals");
    expect(src).toContain("info.notice");
    expect(src).toContain("info.failure");
    expect(src).toContain("info.noticePhase");
    expect(src).toContain("openPhase");
    expect(src.indexOf('v-if="info.notice"')).toBeLessThan(
      src.indexOf('v-else-if="info.failure'),
    );
  });

  test("reads only its props", () => {
    expect(src).not.toContain("fetch(");
    expect(src).not.toContain("lib/api");
    expect(src).not.toContain("EventSource");
  });
});

describe("chip scope", () => {
  const capRule = (blocks: { scoped: boolean; css: string }[]) =>
    blocks.some((b) =>
      cssRules(b.css).some(
        (r) => /\.chip\b/.test(r.selector) && /max-width/.test(r.body),
      ),
    );

  test("style.css has no global chip cap", () => {
    const css = readFileSync(join(import.meta.dir, "../style.css"), "utf8");
    expect(capRule([{ scoped: false, css }])).toBe(false);
  });

  test("no unscoped component chip cap", () => {
    for (const name of readdirSync(COMPONENTS)) {
      if (!name.endsWith(".vue")) continue;
      const blocks = styleBlocks(readComponent(name));
      const unscoped = blocks.filter((b) => !b.scoped);
      expect(capRule(unscoped)).toBe(false);
    }
  });

  test("FocusTab's cap exists but stays scoped", () => {
    const blocks = styleBlocks(readComponent("FocusTab.vue"));
    const chip = blocks
      .filter((b) => b.scoped)
      .flatMap((b) => cssRules(b.css))
      .find((r) => r.selector === ".chip");
    expect(chip?.body).toContain("max-width");
  });
});
