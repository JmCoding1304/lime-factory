import { describe, expect, test } from "bun:test";
import type { EventRow } from "./types";
import { runSignals } from "./signals";

function ev(over: Partial<EventRow>): EventRow {
  return {
    rowid: 1,
    event_id: 1,
    run_id: "r1",
    phase_id: null,
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

function signal(
  eventId: number,
  type: EventRow["type"],
  name: string,
  payload: Record<string, unknown> = {},
): EventRow {
  return ev({
    event_id: eventId,
    type,
    name,
    payload_json: JSON.stringify(payload),
  });
}

describe("runSignals", () => {
  test("no signal events gives an empty list", () => {
    expect(
      runSignals([
        signal(1, "turn", "s1", { cost: 0.01 }),
        signal(2, "phase_end", "build"),
      ]),
    ).toEqual([]);
  });

  test("one entry per type present, in the fixed order", () => {
    // Given out of order on purpose.
    const out = runSignals([
      signal(1, "crash_detected", "build", { signal: 9, exit_code: -9 }),
      signal(2, "timeout_kill", "builder", { elapsed_s: 900 }),
      signal(3, "budget_warn", "builder", { context_tokens: 100 }),
      signal(4, "budget_warn", "reviewer", { context_tokens: 120 }),
      signal(5, "budget_stop", "builder", { context_tokens: 150 }),
    ]);
    expect(out.map((s) => s.type)).toEqual([
      "budget_warn",
      "budget_stop",
      "timeout_kill",
      "crash_detected",
    ]);
    expect(out[0]!.label).toBe("▲ warn ×2");
    expect(out[0]!.count).toBe(2);
    expect(out.slice(1).every((s) => s.label !== "" && !s.label.includes("×"))).toBe(
      true,
    );
  });

  test("tones are warn then bad", () => {
    const out = runSignals([
      signal(1, "budget_warn", "builder"),
      signal(2, "budget_stop", "builder"),
      signal(3, "timeout_kill", "builder", { elapsed_s: 60 }),
      signal(4, "crash_detected", "build", { signal: 9, exit_code: -9 }),
    ]);
    expect(out.map((s) => s.tone)).toEqual(["warn", "bad", "bad", "bad"]);
  });

  test("the crash title names the signal and exit code", () => {
    const [crash] = runSignals([
      signal(1, "crash_detected", "build", { signal: 9, exit_code: -9 }),
    ]);
    expect(crash!.title).toContain("signal 9 · exit -9");
    expect(crash!.title).toContain("build");
  });

  test("a repeated timeout kill says so", () => {
    const [kill] = runSignals([
      signal(1, "timeout_kill", "builder", { elapsed_s: 900, reason: "repeat" }),
    ]);
    expect(kill!.title).toContain("900s");
    expect(kill!.title).toContain("(repeat)");
  });

  test("unrelated event types are ignored", () => {
    expect(runSignals([signal(1, "budget_kill", "builder")])).toEqual([]);
    expect(runSignals([signal(1, "turn", "s1", { cost: 0.02 })])).toEqual([]);
  });
});
