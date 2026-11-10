import { describe, expect, test } from "bun:test";
import type { SessionCost } from "./types";
import * as costLib from "./cost";
import {
  isWin,
  modelStats,
  rankModels,
  rankUsage,
  rankSpeed,
  bySpeed,
  roleCards,
  fmtUsd,
  median,
} from "./cost";

const BASE: SessionCost = {
  project: "test",
  run_id: "R1",
  run_status: "success",
  role: "planner",
  runtime: "pi",
  model: "openrouter/anthropic/claude-opus-5",
  api_cost: 0.5,
  notional_cost: 0,
  tokens: 1000,
  duration_ms: 60_000,
};

function row(overrides: Partial<SessionCost>): SessionCost {
  return { ...BASE, ...overrides };
}

// Helper to create a planner row with claude_code runtime
function subRow(overrides: Partial<SessionCost>): SessionCost {
  return row({ runtime: "claude_code", api_cost: 0, role: "planner", ...overrides });
}

// Helper for non-sub rows of a given role
function apiRow(overrides: Partial<SessionCost>): SessionCost {
  return row({ runtime: "pi", ...overrides });
}

// ── Data for multi-test scenarios ────────────────────────────────────────────

// Three run ids across planner (sub), builder (api), reviewer (api)
// R1: planner sub success, builder api fail, reviewer api success
// R2: planner sub success, builder api success, reviewer api success
// R3: planner sub fail, builder api deployed, reviewer api budget_exhausted

const SCENARIO_ROWS: SessionCost[] = [
  // === R1 ===
  // planner: claude-fable-5-1 sub, success
  subRow({ run_id: "R1", run_status: "success", model: "claude-fable-5-1", notional_cost: 2.0, tokens: 5000 }),
  // builder: deepseek v4, fail
  apiRow({ run_id: "R1", run_status: "fail", role: "builder", model: "openrouter/deepseek-v4", api_cost: 0.1, tokens: 2000 }),
  // reviewer: grok, success
  apiRow({ run_id: "R1", run_status: "success", role: "reviewer", model: "openrouter/grok-3", api_cost: 0.3, tokens: 1500 }),

  // === R2 ===
  // planner: claude-fable-5-1 sub, success
  subRow({ run_id: "R2", run_status: "success", model: "claude-fable-5-1", notional_cost: 1.5, tokens: 4000 }),
  // builder: deepseek v4, success
  apiRow({ run_id: "R2", run_status: "success", role: "builder", model: "openrouter/deepseek-v4", api_cost: 0.2, tokens: 3000 }),
  // reviewer: grok, success
  apiRow({ run_id: "R2", run_status: "success", role: "reviewer", model: "openrouter/grok-3", api_cost: 0.25, tokens: 1200 }),

  // === R3 ===
  // planner: claude-fable-5-1 sub, fail
  subRow({ run_id: "R3", run_status: "fail", model: "claude-fable-5-1", notional_cost: 0.8, tokens: 3000 }),
  // builder: deepseek v4, deployed (= win)
  apiRow({ run_id: "R3", run_status: "deployed", role: "builder", model: "openrouter/deepseek-v4", api_cost: 0.15, tokens: 2500 }),
  // reviewer: grok, budget_exhausted (not a win)
  apiRow({ run_id: "R3", run_status: "budget_exhausted", role: "reviewer", model: "openrouter/grok-3", api_cost: 0.1, tokens: 800 }),
];

// ── Tests ────────────────────────────────────────────────────────────────────

describe("isWin helper", () => {
  test("isWin recognises success and deployed", () => {
    expect(isWin("success")).toBe(true);
    expect(isWin("deployed")).toBe(true);
    expect(isWin("fail")).toBe(false);
    expect(isWin("budget_exhausted")).toBe(false);
    expect(isWin("needs_rescope")).toBe(false);
    expect(isWin("running")).toBe(false);
    expect(isWin("discarded")).toBe(false);
    expect(isWin(null)).toBe(false);
  });
});

describe("OFF: subscription model is unpriced", () => {
  // notionalOn = false → sub model's priced is null, perWin null
  const rows: SessionCost[] = [
    subRow({ run_id: "R1", run_status: "success", model: "claude-fable-5-1", notional_cost: 2.0 }),
  ];
  const stats = modelStats(rows, false);

  test("priced is null (unpriced)", () => {
    const s = stats.find((m) => m.model === "claude-fable-5-1");
    expect(s).not.toBeNull();
    expect(s!.priced).toBeNull();
    expect(s!.sub).toBe(true);
  });

  test("perWin is null", () => {
    const s = stats.find((m) => m.model === "claude-fable-5-1");
    expect(s!.perWin).toBeNull();
  });

  test("rankModels puts unpriced last", () => {
    // Add a priced model too
    const allRows = [
      ...rows,
      apiRow({ run_id: "R1", run_status: "success", model: "openrouter/deepseek-v4", api_cost: 0.5 }),
    ];
    const ranked = rankModels(allRows, false);
    // unpriced should be last
    expect(ranked[ranked.length - 1].priced).toBeNull();
  });

  test("best is not the sub model when OFF", () => {
    const cards = roleCards(rows, false);
    // planner card should have no best (only sub model with null priced)
    const plannerCard = cards.find((c) => c.role === "planner");
    expect(plannerCard).toBeDefined();
    expect(plannerCard!.best).toBeNull();
  });
});

describe("ON: subscription model is priced at notional", () => {
  const rows: SessionCost[] = [
    subRow({ run_id: "R1", run_status: "success", model: "claude-fable-5-1", notional_cost: 2.0 }),
    subRow({ run_id: "R2", run_status: "success", model: "claude-fable-5-1", notional_cost: 1.5 }),
  ];
  const stats = modelStats(rows, true);

  test("priced = sum of notional", () => {
    const s = stats.find((m) => m.model === "claude-fable-5-1");
    expect(s).not.toBeNull();
    expect(s!.priced).toBeCloseTo(3.5, 5);
    expect(s!.api).toBe(0);
    expect(s!.sub).toBe(true);
  });

  test("perWin = priced / wins", () => {
    const s = stats.find((m) => m.model === "claude-fable-5-1");
    expect(s!.wins).toBe(2);
    expect(s!.perWin).toBeCloseTo(1.75, 5);
  });
});

describe("ranking is $ per win descending, 0-win first", () => {
  const rows: SessionCost[] = [
    apiRow({ run_id: "R1", run_status: "success", model: "openrouter/A", api_cost: 5.0 }),
    apiRow({ run_id: "R2", run_status: "success", model: "openrouter/B", api_cost: 3.0 }),
    apiRow({ run_id: "R3", run_status: "fail", model: "openrouter/C", api_cost: 1.0 }),
  ];
  const ranked = rankModels(rows, true);

  test("priced with 0 win ranks first", () => {
    // C has 0 wins (fail) → should be first
    expect(ranked[0].model).toBe("openrouter/C");
    expect(ranked[0].wins).toBe(0);
  });

  test("then perWin descending", () => {
    // Then A (5/1=5) then B (3/1=3)
    expect(ranked[1].model).toBe("openrouter/A");
    expect(ranked[2].model).toBe("openrouter/B");
  });
});

describe("run counts once per model even with two Sessions", () => {
  const rows: SessionCost[] = [
    apiRow({ run_id: "R1", run_status: "success", role: "builder", model: "openrouter/deepseek-v4", api_cost: 0.2 }),
    apiRow({ run_id: "R1", run_status: "success", role: "builder", model: "openrouter/deepseek-v4", api_cost: 0.3 }),
  ];
  const stats = modelStats(rows, true, "builder");

  test("runs is 1 (distinct run_id)", () => {
    const s = stats.find((m) => m.model === "openrouter/deepseek-v4");
    expect(s).not.toBeNull();
    expect(s!.runs).toBe(1);
  });

  test("api is summed across both Sessions", () => {
    const s = stats.find((m) => m.model === "openrouter/deepseek-v4");
    expect(s!.api).toBeCloseTo(0.5, 5);
  });
});

describe("null notional contributes 0", () => {
  const rows: SessionCost[] = [
    subRow({ run_id: "R1", run_status: "success", model: "claude-fable-5-1", notional_cost: null }),
    subRow({ run_id: "R2", run_status: "success", model: "claude-fable-5-1", notional_cost: 2.0 }),
  ];
  const stats = modelStats(rows, true);

  test("notional sum counts null as 0", () => {
    const s = stats.find((m) => m.model === "claude-fable-5-1");
    expect(s!.notional).toBe(2.0);
    expect(s!.runs).toBe(2);
  });
});

describe("role cards ordering and best value", () => {
  const cards = roleCards(SCENARIO_ROWS, true);

  test("roles in ROLE_ORDER first, unknown last", () => {
    const roleNames = cards.map((c) => c.role);
    // planner is first, builder second, reviewer third
    expect(roleNames.indexOf("planner")).toBeLessThan(roleNames.indexOf("builder"));
    expect(roleNames.indexOf("builder")).toBeLessThan(roleNames.indexOf("reviewer"));
  });

  test("best value needs MIN_RUNS and a win", () => {
    const builderCard = cards.find((c) => c.role === "builder");
    // deepseek-v4 has 3 runs (R1,R2,R3), 2 wins (R2 success, R3 deployed)
    // priced = 0.45, perWin = 0.225 — should be best
    expect(builderCard).toBeDefined();
    expect(builderCard!.best).toBe("openrouter/deepseek-v4");
  });

  test("planner card best is null because only sub model (ON)", () => {
    // planner has only claude-fable-5-1 which is sub — it is priced when ON
    // runs=3, wins=2 (R1,R2 success, R3 fail)
    // runs >= 2, wins >= 1, priced is notional sum=4.3, perWin=2.15
    // Since it's the only one, it should be best
    const plannerCard = cards.find((c) => c.role === "planner");
    expect(plannerCard).toBeDefined();
    // claude-fable-5-1 is priced (notionalOn=true), has runs=3, wins=2
    // it should be best
    expect(plannerCard!.best).toBe("claude-fable-5-1");
  });
});

describe("fmtUsd shows sub-cent values as <$0.01", () => {
  test("values >= 0.01 display normally", () => {
    expect(fmtUsd(0.01)).toBe("$0.01");
    expect(fmtUsd(1.5)).toBe("$1.50");
    expect(fmtUsd(0.1)).toBe("$0.10");
  });

  test("values < 0.01 display as <$0.01", () => {
    expect(fmtUsd(0.009)).toBe("<$0.01");
    expect(fmtUsd(0.005)).toBe("<$0.01");
    expect(fmtUsd(0)).toBe("<$0.01");
  });
});

describe("median", () => {
  test("odd count returns the middle value", () => {
    expect(median([1, 2, 3])).toBe(2);
  });

  test("even count returns mean of the two middles", () => {
    expect(median([1, 2, 3, 4])).toBe(2.5);
  });

  test("single element returns that element", () => {
    expect(median([42])).toBe(42);
  });

  test("empty array returns null", () => {
    expect(median([])).toBeNull();
  });
});

describe("durations per model", () => {
  test("four rows of one model with duration_ms 120_000, 60_000, null, 180_000 → timed 3, medianMs 120_000, minMs 60_000, maxMs 180_000", () => {
    const rows: SessionCost[] = [
      row({ run_id: "R1", model: "openrouter/test-model", duration_ms: 120_000 }),
      row({ run_id: "R2", model: "openrouter/test-model", duration_ms: 60_000 }),
      row({ run_id: "R3", model: "openrouter/test-model", duration_ms: null }),
      row({ run_id: "R4", model: "openrouter/test-model", duration_ms: 180_000 }),
    ];
    const stats = modelStats(rows, false);
    const s = stats.find((m) => m.model === "openrouter/test-model");
    expect(s).not.toBeNull();
    expect(s!.timed).toBe(3);
    expect(s!.medianMs).toBe(120_000);
    expect(s!.minMs).toBe(60_000);
    expect(s!.maxMs).toBe(180_000);
  });

  test("all-null → timed 0, medianMs/minMs/maxMs null", () => {
    const rows: SessionCost[] = [
      row({ run_id: "R1", model: "openrouter/test-model", duration_ms: null }),
      row({ run_id: "R2", model: "openrouter/test-model", duration_ms: null }),
    ];
    const stats = modelStats(rows, false);
    const s = stats.find((m) => m.model === "openrouter/test-model");
    expect(s).not.toBeNull();
    expect(s!.timed).toBe(0);
    expect(s!.medianMs).toBeNull();
    expect(s!.minMs).toBeNull();
    expect(s!.maxMs).toBeNull();
  });
});

describe("rankUsage", () => {
  test("sub model OFF → priced null ranks last", () => {
    const rows: SessionCost[] = [
      subRow({ run_id: "R1", model: "sub-model", notional_cost: 5.0 }),
      apiRow({ run_id: "R2", model: "openrouter/api-model-a", api_cost: 3.0 }),
      apiRow({ run_id: "R3", model: "openrouter/api-model-b", api_cost: 1.0 }),
    ];
    const ranked = rankUsage(rows, false);
    // sub-model is unpriced (null), should be last
    expect(ranked[ranked.length - 1].model).toBe("sub-model");
    expect(ranked[ranked.length - 1].priced).toBeNull();
  });

  test("two API models rank priced desc", () => {
    const rows: SessionCost[] = [
      apiRow({ run_id: "R1", model: "openrouter/model-a", api_cost: 2.0 }),
      apiRow({ run_id: "R2", model: "openrouter/model-b", api_cost: 5.0 }),
    ];
    const ranked = rankUsage(rows, false);
    expect(ranked[0].model).toBe("openrouter/model-b");
    expect(ranked[1].model).toBe("openrouter/model-a");
  });
});

describe("rankSpeed / bySpeed", () => {
  test("slowest first on the chart, fastest first in bySpeed, null-median last in both", () => {
    const rows: SessionCost[] = [
      row({ run_id: "R1", model: "fast", duration_ms: 30_000 }),
      row({ run_id: "R2", model: "slow", duration_ms: 120_000 }),
      row({ run_id: "R3", model: "medium", duration_ms: 60_000 }),
      row({ run_id: "R4", model: "no-dur", duration_ms: null }),
    ];
    const ranked = rankSpeed(rows, false);
    // rankSpeed: medianMs desc, null last
    expect(ranked[0].model).toBe("slow");
    expect(ranked[1].model).toBe("medium");
    expect(ranked[2].model).toBe("fast");
    expect(ranked[3].model).toBe("no-dur");

    // bySpeed: medianMs asc, null last
    const sorted = bySpeed(ranked);
    expect(sorted[0].model).toBe("fast");
    expect(sorted[1].model).toBe("medium");
    expect(sorted[2].model).toBe("slow");
    expect(sorted[3].model).toBe("no-dur");
  });

  test("bySpeed does not mutate its input", () => {
    const rows: SessionCost[] = [
      row({ run_id: "R1", model: "fast", duration_ms: 30_000 }),
      row({ run_id: "R2", model: "slow", duration_ms: 120_000 }),
    ];
    const original = rankSpeed(rows, false);
    const originalOrder = original.map((s) => s.model);
    bySpeed(original);
    expect(original.map((s) => s.model)).toEqual(originalOrder);
  });
});

describe("fastest needs MIN_RUNS timed Sessions", () => {
  test("role with model A (2 runs, durations 90 s/90 s) and B (2 runs, one null, one 30 s) → fastest A", () => {
    const rows: SessionCost[] = [
      // model-a: planner, 2 runs both with duration 90s
      row({ run_id: "R1", role: "planner", model: "model-a", duration_ms: 90_000 }),
      row({ run_id: "R2", role: "planner", model: "model-a", duration_ms: 90_000 }),
      // model-b: planner, 2 runs, one null, one 30s => timed=1 < MIN_RUNS
      row({ run_id: "R3", role: "planner", model: "model-b", duration_ms: null }),
      row({ run_id: "R4", role: "planner", model: "model-b", duration_ms: 30_000 }),
    ];
    const cards = roleCards(rows, false);
    const plannerCard = cards.find((c) => c.role === "planner");
    expect(plannerCard).toBeDefined();
    expect(plannerCard!.fastest).toBe("model-a");
  });

  test("B alone → null", () => {
    const rows: SessionCost[] = [
      row({ run_id: "R3", role: "planner", model: "model-b", duration_ms: null }),
      row({ run_id: "R4", role: "planner", model: "model-b", duration_ms: 30_000 }),
    ];
    const cards = roleCards(rows, false);
    const plannerCard = cards.find((c) => c.role === "planner");
    expect(plannerCard).toBeDefined();
    expect(plannerCard!.fastest).toBeNull();
  });

  test("sub model with notionalOn=false still eligible", () => {
    const rows: SessionCost[] = [
      subRow({ run_id: "R1", role: "planner", model: "sub-model", duration_ms: 50_000 }),
      subRow({ run_id: "R2", role: "planner", model: "sub-model", duration_ms: 60_000 }),
    ];
    const cards = roleCards(rows, false);
    const plannerCard = cards.find((c) => c.role === "planner");
    expect(plannerCard).toBeDefined();
    expect(plannerCard!.fastest).toBe("sub-model");
  });
});

describe("planner verdict is gone (ticket 58)", () => {
  test("cost.ts no longer exports plannerVerdict or VERDICT_EDGE_PT", () => {
    expect("plannerVerdict" in costLib).toBe(false);
    expect("VERDICT_EDGE_PT" in costLib).toBe(false);
  });
});