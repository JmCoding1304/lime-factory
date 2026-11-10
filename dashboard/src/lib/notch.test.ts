import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { LiveRunning, RunningRun } from "./types";
import type { LiveBatch, QueuedChild } from "./notch";
import {
  HOT_PCT, batchHeader, childTitle, clockHHMM, compactParts, ctxFill, fmtElapsed,
  notchGroups, notchRoom, notchView, phaseLine, rowTitle, stageOf, waitWord,
} from "./notch";

function run(over: Partial<RunningRun> = {}): RunningRun {
  return {
    project: "p", run_id: "r", kind: "run", workflow: null, ticket: null,
    batch_id: null, batch_position: null,
    attempt_started_at: null, heartbeat_at: null, phase: null, session: null,
    context: null, last_event_at: null, ...over,
  };
}

function child(over: Partial<QueuedChild> = {}): QueuedChild {
  return { project: "p", batch_id: "b", position: 0, ticket: "240-a", reason: null, ...over };
}

function batch(over: Partial<LiveBatch> = {}): LiveBatch {
  return { project: "p", batch_id: "b", retry_of: null, died_at: null, ...over };
}

describe("notchView", () => {
  test("a null frame and an empty list both yield the tiny pill", () => {
    for (const frame of [null, { runs: [] } as LiveRunning]) {
      const v = notchView(frame);
      expect(v.first).toBeNull();
      expect(v.more).toBe(0);
      expect(v.rows.length).toBe(0);
    }
  });

  test("three runs keep feed order with first and more", () => {
    const a = run({ run_id: "a" });
    const b = run({ run_id: "b" });
    const c = run({ run_id: "c" });
    const v = notchView({ runs: [a, b, c] });
    expect(v.first).toBe(a);
    expect(v.more).toBe(2);
    expect(v.rows.map((r) => r.run_id)).toEqual(["a", "b", "c"]);
  });

  test("groups match notchGroups while the counts stay running-only", () => {
    const a = run({ run_id: "a" });
    const frame: LiveRunning = {
      runs: [a],
      queued: [child({ batch_id: "b1" })],
      batches: [batch({ batch_id: "b1" })],
    };
    const v = notchView(frame);
    expect(v.groups).toEqual(notchGroups(frame));
    expect(v.rows.map((r) => r.run_id)).toEqual(["a"]);
    expect(v.first).toBe(a);
    expect(v.more).toBe(0);
  });
});

describe("ctxFill", () => {
  const ctx = (tokens: number | null, warn: number | null) => ({ tokens, warn, stop: null });

  test("full at 100k, clamped above", () => {
    expect(ctxFill(ctx(100_000, 100_000))).toEqual({ pct: 100, hot: true });
    expect(ctxFill(ctx(150_000, 100_000))).toEqual({ pct: 100, hot: true });
  });

  test("half is not hot, the threshold is", () => {
    expect(ctxFill(ctx(50_000, 100_000))).toEqual({ pct: 50, hot: false });
    expect(ctxFill(ctx(80_000, 100_000))).toEqual({ pct: 80, hot: true });
    expect(ctxFill(ctx(79_999, 100_000))).toEqual({ pct: 79.999, hot: false });
  });

  test("warn 200k with 150k tokens is 75 — no 150k ceiling", () => {
    expect(ctxFill(ctx(150_000, 200_000))).toEqual({ pct: 75, hot: false });
  });

  test("warn null, warn 0, tokens null and ctx null are all empty", () => {
    expect(ctxFill(ctx(100_000, null))).toEqual({ pct: 0, hot: false });
    expect(ctxFill(ctx(100_000, 0))).toEqual({ pct: 0, hot: false });
    expect(ctxFill(ctx(null, 100_000))).toEqual({ pct: 0, hot: false });
    expect(ctxFill(null)).toEqual({ pct: 0, hot: false });
  });
});

describe("fmtElapsed", () => {
  const t0 = Date.parse("2030-01-01T00:00:00.000Z");

  test("minutes and seconds", () => {
    expect(fmtElapsed("2030-01-01T00:00:00.000Z", t0 + 65_000)).toBe("1:05");
  });

  test("hours from one up", () => {
    expect(fmtElapsed("2030-01-01T00:00:00.000Z", t0 + 3_725_000)).toBe("1:02:05");
  });

  test("null and unparsable read as a dash", () => {
    expect(fmtElapsed(null, t0)).toBe("—");
    expect(fmtElapsed("nope", t0)).toBe("—");
  });

  test("a future start is zero", () => {
    expect(fmtElapsed("2030-01-01T00:00:05.000Z", t0)).toBe("0:00");
  });
});

describe("phaseLine", () => {
  const full = run({
    phase: { seq: 1, name: "build_1", stage: "Build", status: "running" },
    session: { role: "builder", model: "openrouter/deepseek/deepseek-v4.1-flash" },
  });

  test("phase · role · model", () => {
    expect(phaseLine(full)).toBe("build_1 · builder · deepseek-v4.1-flash");
  });

  test("model dropped when withModel is false", () => {
    expect(phaseLine(full, false)).toBe("build_1 · builder");
  });

  test("no session leaves the phase", () => {
    expect(phaseLine(run({ phase: { seq: 1, name: "build_1", stage: "Build", status: "running" } }))).toBe("build_1");
  });

  test("a bare deploy says deploy, anything else starting", () => {
    expect(phaseLine(run({ kind: "deploy" }))).toBe("deploy");
    expect(phaseLine(run())).toBe("starting");
  });
});

describe("stageOf", () => {
  test("a Build phase is Build", () => {
    expect(stageOf(run({ phase: { seq: 1, name: "build_1", stage: "Build", status: "running" } }))).toBe("Build");
  });

  test("a deploy is Ship with or without a phase stage", () => {
    expect(stageOf(run({ kind: "deploy", phase: { seq: 1, name: "deploy", stage: "Ship", status: "running" } }))).toBe("Ship");
    expect(stageOf(run({ kind: "deploy" }))).toBe("Ship");
  });

  test("a non-deploy without a phase is null", () => {
    expect(stageOf(run())).toBeNull();
  });
});

describe("rowTitle", () => {
  test("a ticket path gives its number and stem", () => {
    expect(rowTitle(run({ ticket: ".lime/tickets/261-notch.md" }))).toEqual({ num: "261", text: "Notch" });
  });

  test("a null ticket falls back to the run_id", () => {
    expect(rowTitle(run({ run_id: "run-9" }))).toEqual({ num: null, text: "run-9" });
  });
});

describe("notchRoom / compactParts", () => {
  test("room for the centred pill", () => {
    expect(notchRoom(1200, 420)).toBe(348);
  });

  test("room never goes below the tiny width", () => {
    expect(notchRoom(0, 1000)).toBe(150);
  });

  test("compact parts drop as room shrinks", () => {
    expect(HOT_PCT).toBe(80);
    expect(compactParts(600)).toEqual({ project: true, model: true });
    expect(compactParts(500)).toEqual({ project: true, model: false });
    expect(compactParts(400)).toEqual({ project: false, model: false });
  });
});

test("notch.ts carries no fixed ceiling", () => {
  const src = readFileSync(join(import.meta.dir, "notch.ts"), "utf8");
  expect(/150[_,]?000|150k/.test(src)).toBe(false);
});

describe("waitWord", () => {
  const table: [string | null, string | null][] = [
    ["slot", "next"],
    ["blocked 245", "after 245"],
    ["blocked 245, 246", "after 245, 246"],
    ["blocked notes.md", "after notes.md"],
    ["overlap lime/feature/240-x", "held"],
    ["overlap 3f2a91bc, lime/feature/9-y", "held"],
    [null, null],
    ["no ticket file", null],
    ["overlap check failed", null],
    ["gave up: blocked 245", null],
    ["gave up", null],
    ["mystery reason", null],
  ];
  test.each(table)("%p → %p", (reason, want) => {
    expect(waitWord(reason)).toBe(want);
  });
});

describe("batchHeader", () => {
  test("a first try reads batch", () => {
    expect(batchHeader(batch())).toBe("batch");
  });

  test("a retry carries the dead batch's local last beat", () => {
    const iso = new Date(2026, 10, 6, 14, 2, 30).toISOString();
    expect(batchHeader(batch({ retry_of: "d", died_at: iso }))).toBe(
      "batch · retried · first try died 14:02",
    );
  });

  test("the tracer's +00:00 microsecond form reads the same", () => {
    const iso = new Date(2026, 10, 6, 14, 2, 30).toISOString();
    const tracer = iso.replace("Z", "+00:00").replace(".000", ".123456");
    expect(batchHeader(batch({ retry_of: "d", died_at: tracer }))).toBe(
      "batch · retried · first try died 14:02",
    );
  });

  test("a retry with no or unparseable beat drops the clock", () => {
    expect(batchHeader(batch({ retry_of: "d", died_at: null }))).toBe("batch · retried");
    expect(batchHeader(batch({ retry_of: "d", died_at: "nope" }))).toBe("batch · retried");
  });
});

describe("clockHHMM", () => {
  test("local clock, zero-padded", () => {
    const iso = new Date(2026, 10, 6, 9, 7, 0).toISOString();
    expect(clockHHMM(iso)).toBe("09:07");
    expect(clockHHMM(null)).toBeNull();
    expect(clockHHMM("nope")).toBeNull();
  });
});

describe("childTitle", () => {
  test("a stem gives its number and humanized text", () => {
    expect(childTitle(child({ ticket: "245-foo-bar" }))).toEqual({ num: "245", text: "Foo bar" });
  });
});

describe("notchGroups", () => {
  test("a null frame and an empty frame are no groups", () => {
    expect(notchGroups(null)).toEqual([]);
    expect(notchGroups({ runs: [] })).toEqual([]);
  });

  test("no batches key keeps today's flat loose list", () => {
    const a = run({ run_id: "a" });
    const b = run({ run_id: "b" });
    expect(notchGroups({ runs: [a, b] })).toEqual([
      {
        key: "loose",
        header: null,
        items: [
          { type: "run", key: "run:p:a", run: a },
          { type: "run", key: "run:p:b", run: b },
        ],
      },
    ]);
  });

  test("a batch groups its runs and queued children by position", () => {
    const hand = run({ run_id: "h" });
    const r2 = run({ run_id: "r2", batch_id: "b1", batch_position: 2 });
    const r0 = run({ run_id: "r0", batch_id: "b1", batch_position: 0 });
    const c1 = child({ batch_id: "b1", position: 1, ticket: "241-b", reason: "slot" });
    const c3 = child({ batch_id: "b1", position: 3, ticket: "243-d", reason: "blocked 241" });
    const groups = notchGroups({
      runs: [hand, r2, r0],
      queued: [c1, c3],
      batches: [batch({ batch_id: "b1" })],
    });
    expect(groups.map((g) => g.key)).toEqual(["loose", "b:p:b1"]);
    expect(groups[0].items.map((i) => i.key)).toEqual(["run:p:h"]);
    expect(groups[1].header).toBe("batch");
    expect(groups[1].items.map((i) => i.key)).toEqual([
      "run:p:r0", "q:p:b1:1", "run:p:r2", "q:p:b1:3",
    ]);
    expect(
      groups[1].items.map((i) => (i.type === "queued" ? i.word : null)),
    ).toEqual([null, "next", null, "after 241"]);
  });

  test("a retry adopts its dead batch's orphaned runs", () => {
    const deadAt = "2026-11-06T11:02:00.123456+00:00";
    const o1 = run({ run_id: "o1", batch_id: "d1", batch_position: 0 });
    const t1run = run({ run_id: "t1r", batch_id: "t1", batch_position: 1 });
    const q0 = child({ batch_id: "t1", position: 0, ticket: "246-y", reason: "blocked 245" });
    const groups = notchGroups({
      runs: [o1, t1run],
      queued: [q0],
      batches: [batch({ batch_id: "t1", retry_of: "d1", died_at: deadAt })],
    });
    expect(groups.map((g) => g.key)).toEqual(["b:p:t1"]);
    expect((groups[0].header ?? "").startsWith("batch · retried · first try died ")).toBe(true);
    expect(groups[0].items.map((i) => i.key)).toEqual(["run:p:o1", "q:p:t1:0", "run:p:t1r"]);
    expect(groups.some((g) => g.key === "b:p:d1")).toBe(false);
  });

  test("an unknown batch id falls loose and an unknown queued child is dropped", () => {
    const orphan = run({ run_id: "x", batch_id: "gone", batch_position: 0 });
    const stray = child({ batch_id: "gone", position: 0, ticket: "247-x", reason: "slot" });
    const groups = notchGroups({
      runs: [orphan],
      queued: [stray],
      batches: [batch({ batch_id: "b1" })],
    });
    expect(groups.map((g) => g.key)).toEqual(["loose"]);
    expect(groups[0].items.map((i) => i.key)).toEqual(["run:p:x"]);
  });

  test("two projects reusing a batch id stay apart", () => {
    const rp = run({ project: "a", run_id: "1", batch_id: "b1", batch_position: 0 });
    const rq = run({ project: "b", run_id: "2", batch_id: "b1", batch_position: 0 });
    const groups = notchGroups({
      runs: [rp, rq],
      batches: [
        batch({ project: "a", batch_id: "b1" }),
        batch({ project: "b", batch_id: "b1" }),
      ],
    });
    expect(groups.map((g) => g.key)).toEqual(["b:a:b1", "b:b:b1"]);
    expect(groups[0].items.map((i) => i.key)).toEqual(["run:a:1"]);
    expect(groups[1].items.map((i) => i.key)).toEqual(["run:b:2"]);
  });

  test("a live batch with no rows draws no group", () => {
    expect(notchGroups({ runs: [], batches: [batch({ batch_id: "empty" })] })).toEqual([]);
  });
});

describe("Notch.vue", () => {
  const notch = readFileSync(join(import.meta.dir, "../components/Notch.vue"), "utf8");
  const workspace = readFileSync(join(import.meta.dir, "../components/Workspace.vue"), "utf8");

  test("updates from the feed alone", () => {
    expect(notch).not.toContain("fetch(");
    expect(notch).not.toContain("lib/api");
    expect(notch).not.toContain("EventSource");
    expect(notch).not.toContain("refetcher(");
    expect(notch).not.toContain("lib/router");

    const intervals = notch.match(/setInterval\(/g) ?? [];
    const ticks =
      notch.match(
        /setInterval\(\s*\(\)\s*=>\s*\(?\s*nowMs\.value\s*=\s*Date\.now\(\)\s*\)?\s*,\s*CLOCK_MS\s*\)/g,
      ) ?? [];
    expect(intervals.length).toBeGreaterThan(0);
    expect(intervals.length).toBe(ticks.length);

    expect(workspace).toContain("live.running");
    expect(workspace).toContain("<Notch");
  });

  test("never hardcodes 150k", () => {
    expect(/150[_,]?000|150k/.test(notch)).toBe(false);
  });

  test("compact carries every part, idle is a tiny pill", () => {
    for (const part of [
      "pulse",
      "nc-proj",
      "nc-title",
      "nc-phase",
      "nc-time",
      "tbar",
      "nc-more",
      "idle-line",
      "ctxFill(",
      "fmtElapsed(",
      "phaseLine(",
      "rowTitle(",
      "attempt_started_at",
    ]) {
      expect(notch).toContain(part);
    }
  });

  test("expanded lists every row with its stage", () => {
    expect(notch).toContain("v-for");
    expect(notch).toContain("nl-row");
    expect(notch).toContain("stageOf(");
    expect(notch).toContain("Nothing running");
  });

  test("a queued row is dim, opens nothing, and batches get a header", () => {
    for (const part of ["nl-batch", "g.header", "nl-wait", "it.word", "childTitle("]) {
      expect(notch).toContain(part);
    }
    const tag = notch.match(/<div[^>]*nl-wait[^>]*>/)?.[0];
    expect(tag).toBeDefined();
    expect(tag).toContain("@click.stop");
    expect(tag).not.toContain("go(");
    expect(/@click\.stop(?!=)/.test(tag!)).toBe(true);

    expect(notch.match(/go\(/g)?.length).toBe(2);
  });
});
