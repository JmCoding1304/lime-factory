import { describe, expect, test } from "bun:test";
import type { EventRow } from "./types";
import { streamRows } from "./stream";

let _evId = 0;

function ev(partial: Partial<EventRow>): EventRow {
  _evId++;
  return {
    rowid: _evId,
    event_id: partial.event_id ?? _evId,
    run_id: "test-run",
    phase_id: "test-phase",
    parent_id: null,
    type: null,
    name: null,
    payload_json: null,
    tokens: null,
    started_at: null,
    ended_at: null,
    ...partial,
  };
}

describe("streamRows", () => {
  test("rows come out in event_id order", () => {
    const rows = streamRows([
      ev({ event_id: 3 }),
      ev({ event_id: 1 }),
      ev({ event_id: 2 }),
    ]);
    expect(rows.map((r) => r.id)).toEqual([1, 2, 3]);
  });

  test("an agent tool call shows tool, args and result", () => {
    const rows = streamRows([
      ev({
        type: "tool_call",
        name: "bash",
        payload_json: JSON.stringify({
          tool: "bash",
          args: { command: "git log --oneline -5" },
          ok: true,
          result_snippet: "abc",
        }),
      }),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0]!.label).toBe("bash");
    expect(rows[0]!.summary).toContain("git log");
    expect(rows[0]!.detail).toContain("→ abc");
    expect(rows[0]!.ok).toBe(true);
  });

  test("a failed agent tool call is marked not ok", () => {
    const rows = streamRows([
      ev({
        type: "tool_call",
        name: "bash",
        payload_json: JSON.stringify({
          tool: "bash",
          args: { command: "git log --oneline -5" },
          ok: false,
          result_snippet: "error",
        }),
      }),
    ]);
    expect(rows[0]!.ok).toBe(false);
  });

  test("a quality tool call shows its command and exit status", () => {
    const rows = streamRows([
      ev({
        type: "tool_call",
        name: "quality:test",
        payload_json: JSON.stringify({
          command: "uv run pytest -q",
          returncode: 1,
          passed: false,
          output_artifact: "/tmp/a.txt",
        }),
      }),
    ]);
    expect(rows[0]!.label).toBe("quality:test");
    expect(rows[0]!.summary).toBe("uv run pytest -q");
    expect(rows[0]!.detail).toContain("exit 1");
    expect(rows[0]!.detail).toContain("/tmp/a.txt");
    expect(rows[0]!.ok).toBe(false);
  });

  test("a turn is a meter, because the payload carries no text", () => {
    const rows = streamRows([
      ev({
        type: "turn",
        payload_json: JSON.stringify({
          turn_no: 3,
          input: 10000,
          output: 2000,
          cost: 0.11,
        }),
      }),
    ]);
    expect(rows[0]!.label).toBe("turn 3");
    expect(rows[0]!.summary).toContain("in");
    expect(rows[0]!.summary).toContain("out");
    expect(rows[0]!.summary).toContain("$");
  });

  test("a handoff carries the agent's summary and artifacts", () => {
    const rows = streamRows([
      ev({
        type: "handoff",
        name: "agent",
        payload_json: JSON.stringify({
          artifacts: ["/x/plan.md"],
          summary: "Single-step plan.",
        }),
      }),
    ]);
    expect(rows[0]!.summary).toBe("Single-step plan.");
    expect(rows[0]!.detail).toContain("/x/plan.md");
  });

  test("a log prompt becomes an expandable text row", () => {
    const prompt = "A".repeat(300);
    const rows = streamRows([
      ev({
        type: "log",
        name: "prompt",
        payload_json: JSON.stringify({ prompt }),
      }),
    ]);
    expect(rows[0]!.kind).toBe("text");
    expect(rows[0]!.summary.length).toBeLessThanOrEqual(161);
    expect(rows[0]!.detail).toBe(prompt);
  });

  test("a log message is a one-line note with no detail", () => {
    const rows = streamRows([
      ev({
        type: "log",
        name: "log",
        payload_json: JSON.stringify({
          message: "▶ 01 engineer",
          level: "info",
        }),
      }),
    ]);
    expect(rows[0]!.kind).toBe("note");
    expect(rows[0]!.detail).toBeNull();
  });

  test("a failed gate lists its violations", () => {
    const rows = streamRows([
      ev({
        type: "gate_fail",
        name: "gate",
        payload_json: JSON.stringify({
          attempt: 1,
          violations: ["missing test"],
          checks: [],
        }),
      }),
    ]);
    expect(rows[0]!.ok).toBe(false);
    expect(rows[0]!.summary).toContain("missing test");
  });

  test("a passing gate with empty payload_json still renders", () => {
    const rows = streamRows([
      ev({
        type: "gate_pass",
        name: "gate",
        payload_json: "",
      }),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0]!.ok).toBe(true);
  });

  test("an error row is always not ok", () => {
    const rows = streamRows([
      ev({
        type: "error",
        name: "error",
        payload_json: JSON.stringify({ error: "push failed" }),
      }),
    ]);
    expect(rows[0]!.ok).toBe(false);
  });

  test("an unknown type still yields one row", () => {
    const rows = streamRows([
      ev({
        type: null,
        name: "mystery",
      }),
    ]);
    expect(rows).toHaveLength(1);
  });

  test("malformed payload_json never throws", () => {
    const rows = streamRows([
      ev({ type: "turn", payload_json: "{not json" }),
      ev({ type: "tool_call", payload_json: "{not json" }),
      ev({ type: "log", payload_json: "{not json" }),
    ]);
    expect(rows).toHaveLength(3);
    for (const row of rows) {
      expect(row.summary).toBe("");
    }
  });
});