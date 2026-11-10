import { describe, expect, test } from "bun:test";
import type { EventRow, ProviderSpend, Session } from "./types";
import { runCash, spendLine } from "./cash";

function row(
  provider: string,
  cost: number,
  subscription: boolean,
): ProviderSpend {
  return { provider, cost, subscription };
}

function session(sessionId: string, model: string | null): Session {
  return {
    session_id: sessionId,
    run_id: "r1",
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

function turn(name: string, cost: number, raw?: string): EventRow {
  return {
    rowid: 1,
    event_id: 1,
    run_id: "r1",
    phase_id: "p1",
    parent_id: null,
    type: "turn",
    name,
    payload_json: raw ?? JSON.stringify({ cost }),
    tokens: null,
    started_at: null,
    ended_at: null,
  };
}

describe("spendLine", () => {
  test("only cash rows: cash summed, no sub and an empty title", () => {
    const line = spendLine([
      row("openrouter", 0.05, false),
      row("anthropic", 0.1, false),
    ]);
    expect(line.cash).toBeCloseTo(0.15);
    expect(line.sub).toBe(false);
    expect(line.subTitle).toBe("");
  });

  test("a mix sums only the cash rows and names each subscription provider", () => {
    const line = spendLine([
      row("openrouter", 0.12, false),
      row("openai-codex", 0.4, true),
      row("claude_code", 1.2, true),
    ]);
    expect(line.cash).toBeCloseTo(0.12);
    expect(line.sub).toBe(true);
    expect(line.subTitle).toContain("openai-codex $0.40");
    expect(line.subTitle).toContain("claude_code $1.20");
    expect(line.subTitle).toContain("notional, not billed");
  });

  test("no spend: cash 0, no sub", () => {
    const line = spendLine([]);
    expect(line.cash).toBe(0);
    expect(line.sub).toBe(false);
    expect(line.subTitle).toBe("");
  });
});

describe("runCash", () => {
  test("a cash session's turns sum to dollars, no sub", () => {
    const out = runCash(
      [session("s1", "openrouter/deepseek/deepseek-v4.1-flash")],
      [turn("s1", 0.02), turn("s1", 0.03)],
    );
    expect(out.cash).toBeCloseTo(0.05);
    expect(out.sub).toBe(false);
  });

  test("a non-cash session sets sub without touching cash", () => {
    const out = runCash(
      [
        session("s1", "openrouter/deepseek/deepseek-v4.1-flash"),
        session("s2", "openai-codex/gpt-6-astra"),
      ],
      [turn("s1", 0.02), turn("s1", 0.03), turn("s2", 0.4)],
    );
    expect(out.cash).toBeCloseTo(0.05);
    expect(out.sub).toBe(true);
  });

  test("a turn matching no session marks sub and leaves cash unchanged", () => {
    const out = runCash(
      [session("s1", "openrouter/deepseek/deepseek-v4.1-flash")],
      [turn("s1", 0.02), turn("orphan", 0.5)],
    );
    expect(out.cash).toBeCloseTo(0.02);
    expect(out.sub).toBe(true);
  });

  test("a turn with an unparsable payload is ignored", () => {
    const out = runCash(
      [session("s1", "openrouter/deepseek/deepseek-v4.1-flash")],
      [turn("s1", 0, "{not json")],
    );
    expect(out.cash).toBe(0);
    expect(out.sub).toBe(false);
  });
});
