import { describe, expect, test } from "bun:test";
import type { RunStatus } from "./types";
import { IDLE_AFTER_MS, idleLabel, idleMinutes, titleLine } from "./liveness";

const NOW = Date.parse("2026-08-31T12:00:00+00:00");
const MIN = 60_000;

function run(status: RunStatus, lastEventAgoMin: number | null, startedAgoMin = 0) {
  return {
    status,
    last_event_at:
      lastEventAgoMin === null
        ? null
        : new Date(NOW - lastEventAgoMin * MIN).toISOString(),
    started_at: new Date(NOW - startedAgoMin * MIN).toISOString(),
  };
}

describe("idleMinutes", () => {
  test("a running Run idle 9 min is fresh", () => {
    expect(idleMinutes(run("running", 9), NOW)).toBeNull();
  });

  test("a running Run idle 10 min reads 10", () => {
    expect(idleMinutes(run("running", 10), NOW)).toBe(10);
  });

  test("37.5 min idle floors to 37", () => {
    expect(idleMinutes(run("running", 37.5), NOW)).toBe(37);
  });

  test("no last event falls back to started_at", () => {
    expect(idleMinutes(run("running", null, 12), NOW)).toBe(12);
  });

  test("terminal statuses are never idle", () => {
    expect(idleMinutes(run("fail", 60), NOW)).toBeNull();
    expect(idleMinutes(run("success", 60), NOW)).toBeNull();
  });

  test("an unparsable timestamp is null", () => {
    expect(
      idleMinutes({ status: "running", last_event_at: "not-a-date", started_at: null }, NOW),
    ).toBeNull();
  });

  test("the idle threshold is 10 minutes", () => {
    expect(IDLE_AFTER_MS).toBe(10 * MIN);
  });
});

describe("idleLabel", () => {
  test("a running Run idle 37.5 min reads idle 37m", () => {
    expect(idleLabel(run("running", 37.5), NOW)).toBe("idle 37m");
  });

  test("a running Run at 9 min is empty", () => {
    expect(idleLabel(run("running", 9), NOW)).toBe("");
  });

  test("terminal statuses are empty even when idle", () => {
    expect(idleLabel(run("fail", 60), NOW)).toBe("");
    expect(idleLabel(run("success", 60), NOW)).toBe("");
  });
});

describe("titleLine", () => {
  test("counts running and failed", () => {
    const runs: { status: RunStatus }[] = [
      { status: "running" },
      { status: "running" },
      { status: "fail" },
      { status: "budget_exhausted" },
      { status: "success" },
    ];
    expect(titleLine(runs)).toBe("2 running · 1 failed");
  });

  test("empty pool", () => {
    expect(titleLine([])).toBe("0 running · 0 failed");
  });
});
