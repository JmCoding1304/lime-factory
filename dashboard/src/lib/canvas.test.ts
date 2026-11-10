import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Envelope, FeedEvent, Phase, Session } from "./types";
import { ZOOM, fitView } from "./bench";
import { LIME_ICON, commandIcon, modelLogo, modelName } from "./models";
import { fmtDuration } from "./format";
import {
  CARD_H,
  CARD_W,
  type CanvasStage,
  cardsOf,
  contextBar,
  firstCommand,
  backbone,
  layoutCanvas,
  liveAction,
  nextSelection,
  pillFaces,
  runTries,
  stageRow,
  toggleTry,
  tryToOpen,
} from "./canvas";

// ── Fixtures ─────────────────────────────────────────────────────────────────

function phase(over: Partial<Phase> = {}): Phase {
  return {
    phase_id: "p1",
    run_id: "r1",
    seq: 1,
    name: over.phase_id ?? "build_1",
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

function session(over: Partial<Session> = {}): Session {
  return {
    session_id: "s1",
    run_id: "r1",
    phase_id: "p1",
    role: "builder",
    continuation: 0,
    runtime: "pi",
    model: "anthropic/claude-sonnet-4",
    color: null,
    context_tokens: null,
    context_window: null,
    created_at: null,
    last_used_at: null,
    ...over,
  };
}

let evSeq = 0;
function ev(over: Partial<FeedEvent> = {}): FeedEvent {
  evSeq += 1;
  return {
    seq: evSeq,
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

// ── Run 267 (2b70e1b9): a resumed Run with two tries ────────────────────────

const RUN267 = "2b70e1b9";

function id267(seq: number): string {
  return `${RUN267}:${String(seq).padStart(2, "0")}`;
}

function ids267(from: number, to: number): string[] {
  const out: string[] = [];
  for (let s = from; s <= to; s++) out.push(id267(s));
  return out;
}

function at267(hms: string): string {
  return `2026-11-07T${hms}+00:00`;
}

// seq | name | kind | owner | stage | start | end
const ROWS267: [
  number,
  string,
  Phase["kind"],
  string | null,
  string,
  string,
  string,
][] = [
  [1, "engineer", "engineer", "Alex Doe", "Prepare", "04:54:37.761", "04:54:37.765"],
  [2, "setup", "code", "git", "Prepare", "04:54:37.765", "04:54:37.884"],
  [3, "scout", "agent", "scout", "Prepare", "04:54:37.885", "04:56:26.346"],
  [4, "plan", "agent", "planner", "Prepare", "04:56:26.349", "05:06:33.533"],
  [5, "build_1", "agent", "builder", "Build", "05:06:33.536", "05:08:45.797"],
  [6, "verify_1", "code", "git", "Build", "05:08:45.831", "05:12:06.976"],
  [7, "build_2", "agent", "builder", "Build", "05:12:07.019", "05:13:33.675"],
  [8, "verify_2", "code", "git", "Build", "05:13:33.708", "05:17:21.805"],
  [9, "build_3", "agent", "builder", "Build", "05:17:21.842", "05:19:42.819"],
  [10, "verify_3", "code", "git", "Build", "05:19:42.854", "05:22:27.150"],
  [11, "review", "agent", "reviewer", "Review", "05:22:27.185", "05:23:43.080"],
  [12, "revise_1", "agent", "builder", "Review", "05:23:43.080", "05:26:03.009"],
  [13, "verify_3_retry3", "code", "git", "Build", "05:26:03.041", "05:28:42.477"],
  [14, "review_retry1", "agent", "reviewer", "Review", "05:28:42.509", "05:29:52.992"],
  [15, "revise_2", "agent", "builder", "Review", "05:29:52.993", "05:32:10.481"],
  [16, "verify_3_retry4", "code", "git", "Build", "05:32:10.516", "05:34:50.003"],
  [17, "review_retry2", "agent", "reviewer", "Review", "05:34:50.037", "05:35:31.504"],
  [18, "verify_3", "code", "git", "Build", "05:35:58.709", "05:39:44.229"],
  [19, "review", "agent", "reviewer", "Review", "05:39:44.264", "05:40:35.103"],
  [20, "revise_1", "agent", "builder", "Review", "05:40:35.103", "05:41:17.427"],
  [21, "verify_3_retry3", "code", "git", "Build", "05:41:17.460", "05:45:10.991"],
  [22, "review_retry1", "agent", "reviewer", "Review", "05:45:11.029", "05:46:16.632"],
  [23, "revise_2", "agent", "builder", "Review", "05:46:16.640", "05:47:56.184"],
  [24, "verify_3_retry4", "code", "git", "Build", "05:47:56.215", "05:51:42.278"],
  [25, "review_retry2", "agent", "reviewer", "Review", "05:51:42.311", "05:52:20.632"],
  [26, "commit_code", "code", "git", "Review", "05:52:20.632", "05:52:20.753"],
  [27, "changes", "code", "git", "Review", "05:52:20.785", "05:52:20.916"],
  [28, "document", "agent", "documenter", "Ship", "05:52:20.916", "05:55:35.843"],
  [29, "commit_docs", "code", "git", "Ship", "05:55:35.844", "05:55:35.950"],
  [30, "push_pr", "code", "git", "Ship", "05:55:36.154", "05:55:40.131"],
];

function reviewEnvelope(seq: number, approved: boolean, blocking: number): Envelope {
  return {
    envelope_id: seq,
    run_id: RUN267,
    phase_id: id267(seq),
    agent: "reviewer",
    output_type: "ReviewOutput",
    payload_json: JSON.stringify({
      approved,
      blocking: Array.from({ length: blocking }, (_, i) => `blocker ${i + 1}`),
    }),
    valid: 1,
    attempt: 1,
    created_at: at267("05:30:00.000"),
  };
}

function run267() {
  const run = {
    started_at: at267("04:54:37.733"),
    engineer: "Alex Doe",
    status: "deployed",
  };

  const phases: Phase[] = ROWS267.map(
    ([seq, name, kind, owner, stage, start, end]) =>
      phase({
        phase_id: id267(seq),
        run_id: RUN267,
        seq,
        name,
        kind,
        owner,
        stage,
        status: "success",
        attempt: 1,
        retries: 0,
        started_at: at267(start),
        ended_at: at267(end),
      }),
  );

  // the resume writes one replay log per reused phase within milliseconds
  const replayHms = [
    "05:35:58.548",
    "05:35:58.548",
    "05:35:58.548",
    "05:35:58.550",
    "05:35:58.552",
    "05:35:58.585",
    "05:35:58.614",
    "05:35:58.647",
    "05:35:58.676",
  ];
  const events: FeedEvent[] = replayHms.map((hms, i) =>
    ev({
      type: "log",
      name: rows267Name(i + 1),
      phase_id: id267(i + 1),
      payload: { replayed: true },
      started_at: at267(hms),
    }),
  );

  // the phases the resume threw away
  for (let seq = 10; seq <= 17; seq++) {
    events.push(
      ev({
        type: "log",
        name: "phase_invalidated",
        phase_id: id267(seq),
        started_at: at267("05:35:58.440"),
      }),
    );
  }

  // the error that ended try 1, on a phase whose status is success
  events.push(
    ev({
      type: "error",
      phase_id: id267(17),
      payload: {
        reason:
          "review still not approved after 2 revise attempt(s): Existing writers lack a guard",
      },
      started_at: at267("05:35:31.504"),
    }),
  );

  // how try 2 began
  events.push(
    ev({
      type: "log",
      name: "rereview_decision",
      phase_id: null,
      payload: {
        reason: "engineer requested --rereview",
        engineer: "Alex Doe",
        verify: "verify_3",
        seq: 10,
      },
      started_at: at267("05:35:58.443"),
    }),
  );

  const envelopes: Envelope[] = [
    reviewEnvelope(11, false, 2),
    reviewEnvelope(14, false, 1),
    reviewEnvelope(17, false, 1),
    reviewEnvelope(19, false, 1),
    reviewEnvelope(22, false, 1),
    reviewEnvelope(25, true, 0),
  ];

  return { run, phases, events, envelopes };
}

function rows267Name(seq: number): string {
  const row = ROWS267.find((r) => r[0] === seq);
  return row ? row[1] : "";
}

// ── stageRow ─────────────────────────────────────────────────────────────────

const ROW_CASES: [string | null | undefined, CanvasStage][] = [
  ["Prepare", "Prepare"],
  ["Build", "Build"],
  ["Review", "Review"],
  ["Ship", "Ship"],
  ["Other", "Other"],
  [null, "Other"],
  [undefined, "Other"],
  ["Deploy", "Other"],
  ["build", "Other"],
];

describe("stageRow", () => {
  test.each(ROW_CASES)("stageRow(%p) → %p", (stage, expected) => {
    expect(stageRow(stage)).toBe(expected);
  });
});

// ── layoutCanvas: rows ───────────────────────────────────────────────────────

describe("layoutCanvas rows", () => {
  const SINGLE: [string | null, CanvasStage][] = [
    ["Prepare", "Prepare"],
    ["Build", "Build"],
    ["Review", "Review"],
    ["Ship", "Ship"],
    ["Other", "Other"],
    [null, "Other"],
    ["Deploy", "Other"],
  ];

  test.each(SINGLE)("a one-phase Run gets its %p row", (stage, expected) => {
    const layout = layoutCanvas([
      phase({ phase_id: "p1", seq: 1, stage }),
    ]);
    expect(layout.labels).toHaveLength(1);
    expect(layout.labels[0].text).toBe(expected);
  });

  test("row groups keep the Prepare, Build, Review, Ship, Other order", () => {
    const stages = ["Ship", "Prepare", "Other", "Review", "Build"];
    const layout = layoutCanvas(
      stages.map((stage, i) =>
        phase({ phase_id: `p${i + 1}`, seq: i + 1, stage }),
      ),
    );
    expect(layout.labels.map((l) => l.text)).toEqual([
      "Prepare",
      "Build",
      "Review",
      "Ship",
      "Other",
    ]);
    expect(layout.pos["p3"].y).toBeGreaterThan(layout.pos["p1"].y);
  });

  test("no Other row without such a phase", () => {
    const layout = layoutCanvas(
      ["Prepare", "Build", "Review", "Ship"].map((stage, i) =>
        phase({ phase_id: `p${i + 1}`, seq: i + 1, stage }),
      ),
    );
    expect(layout.labels.map((l) => l.text)).not.toContain("Other");
  });

  test("names are never mapped to rows", () => {
    const layout = layoutCanvas([
      phase({ phase_id: "a", seq: 1, name: "build_1", stage: "Review" }),
      phase({ phase_id: "b", seq: 2, name: "review", stage: null }),
    ]);
    // b is in Other (a lower row than Review) despite its name.
    expect(layout.pos["b"].row).toBeGreaterThan(layout.pos["a"].row);
    expect(layout.labels.map((l) => l.text)).toEqual(["Review", "Other"]);
  });

  test("an out-of-order stage lands in its own row", () => {
    const layout = layoutCanvas([
      phase({ phase_id: "r", seq: 5, stage: "Review", name: "review_1" }),
      phase({ phase_id: "b", seq: 6, stage: "Build", name: "build_1" }),
    ]);
    expect(layout.pos["b"].row).toBe(0);
    expect(layout.pos["r"].row).toBe(1);
  });

  test("a group wraps every MAX_COLS and labels once", () => {
    const layout = layoutCanvas(
      Array.from({ length: 8 }, (_, i) =>
        phase({ phase_id: `p${i + 1}`, seq: i + 1, stage: "Build" }),
      ),
    );
    expect(layout.labels).toHaveLength(1);
    expect(layout.labels[0].text).toBe("Build");
    expect(layout.pos["p1"].row).toBe(0);
    expect(layout.pos["p6"].row).toBe(0);
    expect(layout.pos["p7"].row).toBe(1);
    const sixCols = 2 * 72 + 6 * CARD_W + 5 * 56;
    expect(layout.width).toBe(sixCols);
  });
});

// ── layoutCanvas: edges ──────────────────────────────────────────────────────

describe("layoutCanvas edges", () => {
  test("one edge per consecutive pair", () => {
    const layout = layoutCanvas([
      phase({ phase_id: "a", seq: 1, stage: "Build" }),
      phase({ phase_id: "b", seq: 2, stage: "Build" }),
      phase({ phase_id: "c", seq: 3, stage: "Build" }),
    ]);
    expect(layout.edges).toHaveLength(2);
    expect(layout.edges.map((e) => e.id)).toEqual(["b", "c"]);
  });

  test("a same-row edge is a straight horizontal run", () => {
    const layout = layoutCanvas([
      phase({ phase_id: "a", seq: 1, stage: "Build" }),
      phase({ phase_id: "b", seq: 2, stage: "Build" }),
    ]);
    const a = layout.pos["a"];
    const b = layout.pos["b"];
    expect(layout.edges[0].d).toBe(
      `M${a.x + CARD_W + 6} ${a.y + CARD_H / 2}H${b.x - 9}`,
    );
  });

  test("a cross-row edge wraps, going down", () => {
    const layout = layoutCanvas([
      phase({ phase_id: "a", seq: 1, stage: "Build" }),
      phase({ phase_id: "b", seq: 2, stage: "Review" }),
    ]);
    const a = layout.pos["a"];
    const b = layout.pos["b"];
    const d = layout.edges[0].d;
    expect(d.startsWith(`M${a.x + CARD_W + 6} ${a.y + CARD_H / 2}`)).toBe(true);
    expect(d.endsWith(`H${b.x - 9}`)).toBe(true);
    expect(d).toContain("V");
    expect(d).not.toContain("NaN");
  });

  test("a cross-row edge wraps when b sits above a", () => {
    const layout = layoutCanvas([
      phase({ phase_id: "a", seq: 1, stage: "Review" }),
      phase({ phase_id: "b", seq: 2, stage: "Build" }),
    ]);
    const a = layout.pos["a"];
    const b = layout.pos["b"];
    const d = layout.edges[0].d;
    expect(d.startsWith(`M${a.x + CARD_W + 6} ${a.y + CARD_H / 2}`)).toBe(true);
    expect(d.endsWith(`H${b.x - 9}`)).toBe(true);
    expect(d).toContain("V");
    expect(d).not.toContain("NaN");
  });

  test("flags follow the two phases' statuses", () => {
    const layout = layoutCanvas([
      phase({ phase_id: "a", seq: 1, status: "success", stage: "Build" }),
      phase({ phase_id: "b", seq: 2, status: "running", stage: "Build" }),
    ]);
    expect(layout.edges[0].done).toBe(true);
    expect(layout.edges[0].flowing).toBe(true);

    const failed = layoutCanvas([
      phase({ phase_id: "a", seq: 1, status: "success", stage: "Build" }),
      phase({ phase_id: "b", seq: 2, status: "fail", stage: "Build" }),
    ]);
    expect(failed.edges[0].broken).toBe(true);
  });

  test("an empty layout is all zeros", () => {
    const layout = layoutCanvas([]);
    expect(layout.width).toBe(0);
    expect(layout.height).toBe(0);
    expect(layout.bounds).toEqual({ x: 0, y: 0, w: 0, h: 0 });
    expect(layout.labels).toEqual([]);
    expect(layout.edges).toEqual([]);
    expect(layout.pos).toEqual({});
    expect(JSON.stringify(layout)).not.toContain("NaN");
  });
});

// ── fitView ──────────────────────────────────────────────────────────────────

describe("fitView", () => {
  test("fits a 4-stage, 10-phase Run inside the viewport", () => {
    const stages = [
      "Prepare",
      "Build",
      "Review",
      "Ship",
      "Prepare",
      "Build",
      "Review",
      "Ship",
      "Prepare",
      "Build",
    ];
    const layout = layoutCanvas(
      stages.map((stage, i) =>
        phase({ phase_id: `p${i + 1}`, seq: i + 1, stage }),
      ),
    );
    const view = fitView(layout.bounds, 1200, 700, 24);
    for (const p of Object.values(layout.pos)) {
      const sx = p.x * view.k + view.x;
      const sy = p.y * view.k + view.y;
      expect(sx).toBeGreaterThanOrEqual(0);
      expect(sy).toBeGreaterThanOrEqual(0);
      expect(sx + CARD_W * view.k).toBeLessThanOrEqual(1200);
      expect(sy + CARD_H * view.k).toBeLessThanOrEqual(700);
    }
  });

  test("small content stays at k=1 and is centred", () => {
    const content = { x: 10, y: 20, w: 100, h: 80 };
    const view = fitView(content, 800, 600, 24);
    expect(view.k).toBe(1);
    expect(view.x).toBe((800 - content.w) / 2 - content.x);
    expect(view.y).toBe((600 - content.h) / 2 - content.y);
  });

  test("huge content clamps to ZOOM.min", () => {
    const view = fitView({ x: 0, y: 0, w: 100000, h: 100000 }, 800, 600, 24);
    expect(view.k).toBe(ZOOM.min);
  });

  test("zero-size content gives k=1 and finite x/y", () => {
    const view = fitView({ x: 0, y: 0, w: 0, h: 0 }, 800, 600, 24);
    expect(view.k).toBe(1);
    expect(Number.isFinite(view.x)).toBe(true);
    expect(Number.isFinite(view.y)).toBe(true);
  });
});

// ── contextBar ───────────────────────────────────────────────────────────────

describe("contextBar", () => {
  test("fills proportionally to the warn threshold", () => {
    expect(contextBar(60_000, 120_000)?.pct).toBe(50);
    expect(contextBar(60_000, 300_000)?.pct).toBe(20);
  });

  test("marks a bar past 80% as hot", () => {
    expect(contextBar(130_000, 150_000)?.hot).toBe(true);
  });

  test("caps the fill at 100%", () => {
    expect(contextBar(400_000, 100_000)?.pct).toBe(100);
  });

  test("null for missing or zero inputs", () => {
    expect(contextBar(60_000, null)).toBeNull();
    expect(contextBar(60_000, 0)).toBeNull();
    expect(contextBar(null, 120_000)).toBeNull();
    expect(contextBar(0, 120_000)).toBeNull();
  });
});

// ── cardsOf: context ─────────────────────────────────────────────────────────

describe("cardsOf context", () => {
  test("the latest turn wins over older turns and Session tokens", () => {
    const cards = cardsOf(
      [phase({ phase_id: "p1", kind: "agent" })],
      [session({ phase_id: "p1", context_tokens: 1000, budget_warn: 10_000 })],
      [
        ev({ type: "turn", phase_id: "p1", tokens: 5000 }),
        ev({ type: "turn", phase_id: "p1", tokens: 9000 }),
      ],
      0,
    );
    expect(cards[0].ctx?.used).toBe(9000);
  });

  test("falls back to the Session's context_tokens", () => {
    const cards = cardsOf(
      [phase({ phase_id: "p1", kind: "agent" })],
      [session({ phase_id: "p1", context_tokens: 4000, budget_warn: 10_000 })],
      [],
      0,
    );
    expect(cards[0].ctx?.used).toBe(4000);
  });

  test("a Session without budget_warn gives no bar", () => {
    const cards = cardsOf(
      [phase({ phase_id: "p1", kind: "agent" })],
      [session({ phase_id: "p1", context_tokens: 4000 })],
      [],
      0,
    );
    expect(cards[0].ctx).toBeNull();
  });

  test("code phases have no context bar", () => {
    const cards = cardsOf(
      [phase({ phase_id: "p1", kind: "code" })],
      [session({ phase_id: "p1", context_tokens: 4000, budget_warn: 10_000 })],
      [],
      0,
    );
    expect(cards[0].ctx).toBeNull();
  });
});

// ── cardsOf: face ────────────────────────────────────────────────────────────

describe("cardsOf face", () => {
  test("an agent card shows its role and model", () => {
    const m = "anthropic/claude-sonnet-4";
    const cards = cardsOf(
      [phase({ phase_id: "p1", kind: "agent", owner: "builder" })],
      [session({ phase_id: "p1", model: m })],
      [],
      0,
    );
    expect(cards[0].ownerLine).toBe(`builder · ${modelName(m)}`);
    expect(cards[0].icon).toBe(modelLogo(m));
    expect(cards[0].model).toBe(m);
  });

  test("a code card uses the command icon", () => {
    const cards = cardsOf(
      [phase({ phase_id: "p1", kind: "code" })],
      [],
      [
        ev({
          type: "log",
          phase_id: "p1",
          payload: { command: "uv run pytest -q" },
        }),
      ],
      0,
    );
    expect(cards[0].icon).toBe(commandIcon("uv run pytest -q"));
  });

  test("an engineer card uses the lime icon and the engineer name", () => {
    const cards = cardsOf(
      [phase({ phase_id: "p1", kind: "engineer" })],
      [],
      [],
      0,
      "dev",
    );
    expect(cards[0].icon).toBe(LIME_ICON);
    expect(cards[0].ownerLine).toBe("dev");
  });

  test("a null status is queued", () => {
    const cards = cardsOf(
      [phase({ phase_id: "p1", kind: "code", status: null })],
      [],
      [],
      0,
    );
    expect(cards[0].status).toBe("queued");
  });
});

// ── liveAction / firstCommand ────────────────────────────────────────────────

describe("liveAction", () => {
  test("shows the last open tool call, then Thinking when closed", () => {
    const open = [
      ev({ type: "agent_start" }),
      ev({
        type: "tool_call",
        name: "bash",
        payload: { tool_call_id: "1", tool: "bash" },
      }),
    ];
    expect(liveAction(open)).toBe("bash…");

    const closed = [
      ...open,
      ev({ type: "tool_result", payload: { tool_call_id: "1" } }),
    ];
    expect(liveAction(closed)).toBe("Thinking…");
  });

  test("closing the second of two calls shows the first", () => {
    const events = [
      ev({
        type: "tool_call",
        name: "bash",
        payload: { tool_call_id: "1" },
      }),
      ev({
        type: "tool_call",
        name: "read",
        payload: { tool_call_id: "2" },
      }),
      ev({ type: "tool_result", payload: { tool_call_id: "2" } }),
    ];
    expect(liveAction(events)).toBe("bash…");
  });

  test("a fresh agent_start clears open calls", () => {
    const events = [
      ev({ type: "tool_call", name: "bash", payload: { tool_call_id: "1" } }),
      ev({ type: "agent_start" }),
    ];
    expect(liveAction(events)).toBe("Thinking…");
  });
});

describe("firstCommand", () => {
  test("finds the first command in a tool_call or log payload", () => {
    expect(
      firstCommand([
        ev({ type: "turn" }),
        ev({ type: "log", payload: { command: "uv run pytest -q" } }),
      ]),
    ).toBe("uv run pytest -q");
    expect(firstCommand([ev({ type: "log", payload: { other: 1 } })])).toBeNull();
  });
});

// ── cardsOf: time ────────────────────────────────────────────────────────────

describe("cardsOf time", () => {
  test("a running agent shows its live action", () => {
    const cards = cardsOf(
      [phase({ phase_id: "p1", kind: "agent", status: "running" })],
      [],
      [
        ev({ type: "agent_start", phase_id: "p1" }),
        ev({
          type: "tool_call",
          phase_id: "p1",
          name: "bash",
          payload: { tool_call_id: "1" },
        }),
      ],
      0,
    );
    expect(cards[0].time).toBe("bash…");
    expect(cards[0].live).toBe(true);
  });

  test("a finished phase shows its duration", () => {
    const start = "2024-01-01T00:00:00.000Z";
    const end = "2024-01-01T00:05:00.000Z";
    const now = Date.parse("2024-01-01T00:10:00.000Z");
    const cards = cardsOf(
      [phase({ phase_id: "p1", kind: "code", status: "success", started_at: start, ended_at: end })],
      [],
      [],
      now,
    );
    expect(cards[0].time).toBe(fmtDuration(start, end, now));
  });

  test("a queued phase reads queued", () => {
    const cards = cardsOf(
      [phase({ phase_id: "p1", kind: "code", status: "queued" })],
      [],
      [],
      0,
    );
    expect(cards[0].time).toBe("queued");
  });
});

// ── cardsOf: badges ──────────────────────────────────────────────────────────

describe("cardsOf badges", () => {
  test("a budget_warn only marks its own phase", () => {
    const cards = cardsOf(
      [
        phase({ phase_id: "a", seq: 1, kind: "code" }),
        phase({ phase_id: "b", seq: 2, kind: "code" }),
      ],
      [],
      [ev({ type: "budget_warn", phase_id: "a" })],
      0,
    );
    expect(cards.find((c) => c.id === "a")?.warn).toBe(true);
    expect(cards.find((c) => c.id === "b")?.warn).toBe(false);
  });

  test("ord counts a step's runs in its try", () => {
    const cards = cardsOf(
      [
        phase({ phase_id: "a", seq: 1, name: "build_1" }),
        phase({ phase_id: "b", seq: 2, name: "build_1_retry1" }),
      ],
      [],
      [],
      0,
    );
    expect(cards.find((c) => c.id === "a")?.ord).toBe(1);
    expect(cards.find((c) => c.id === "b")?.ord).toBe(2);
  });

  test("a continuation event or Session marks fresh", () => {
    const byEvent = cardsOf(
      [phase({ phase_id: "a", kind: "code" })],
      [],
      [ev({ type: "continuation", phase_id: "a" })],
      0,
    );
    expect(byEvent[0].fresh).toBe(true);

    const bySession = cardsOf(
      [phase({ phase_id: "a", kind: "agent" })],
      [session({ phase_id: "a", continuation: 1 })],
      [],
      0,
    );
    expect(bySession[0].fresh).toBe(true);

    const neither = cardsOf(
      [phase({ phase_id: "a", kind: "code" })],
      [session({ phase_id: "a", continuation: 0 })],
      [],
      0,
    );
    expect(neither[0].fresh).toBe(false);
  });
});

// ── source guard: no fixed context number ────────────────────────────────────

describe("the context bar", () => {
  test("the canvas never measures context against a fixed number", () => {
    const canvas = readFileSync(join(import.meta.dir, "canvas.ts"), "utf8");
    const component = readFileSync(
      join(import.meta.dir, "../components/RunCanvas.vue"),
      "utf8",
    );
    expect(canvas).not.toMatch(/150[_,]?000|150k/i);
    expect(component).not.toMatch(/150[_,]?000|150k/i);
    // the bar goes through the Session-warn path, not a literal threshold
    expect(component).toMatch(/budget_warn|contextBar/);
  });
});

// ── nextSelection ────────────────────────────────────────────────────────────

describe("nextSelection", () => {
  test("selects, toggles off, switches and clears", () => {
    expect(nextSelection(null, { card: "a" })).toBe("a");
    expect(nextSelection("a", { card: "a" })).toBeNull();
    expect(nextSelection("a", { card: "b" })).toBe("b");
    expect(nextSelection("a", "clear")).toBeNull();
    expect(nextSelection(null, "clear")).toBeNull();
  });
});

// ── source guard: the Run page's reveal and one Esc path ─────────────────────

describe("RunCanvas.vue", () => {
  const src = readFileSync(
    join(import.meta.dir, "../components/RunCanvas.vue"),
    "utf8",
  );

  test("reveals a card through the pure helper", () => {
    expect(src).toContain("revealView(");
    const exposed = src.match(/defineExpose\(\{[^}]*\}\)/)?.[0] ?? "";
    expect(exposed).toContain("reveal");
  });

  test("leaves Escape to the shell", () => {
    expect(src).not.toContain("@keydown");
  });

  test("folds tries and labels its cards through the pure helpers", () => {
    expect(src).toContain("toggleTry(");
    expect(src).toContain("tryToOpen(");
    expect(src).toContain("c.label");
    expect(src).not.toContain("c.retry");
    expect(src).not.toContain("⟳");
  });
});

// ── runTries: Run 267 ────────────────────────────────────────────────────────

describe("runTries", () => {
  const { run, phases, events } = run267();
  const tries = runTries(phases, events, run);

  test("a resumed Run has two tries", () => {
    expect(tries).toHaveLength(2);
  });

  test("try 1 is the lime run itself", () => {
    const t = tries[0];
    expect(t.n).toBe(1);
    expect(t.how).toBe("lime run");
    expect(t.who).toBe("Alex Doe");
    expect(t.start).toBe(run.started_at);
    expect(t.own).toEqual(ids267(1, 17));
    expect(t.reused).toEqual([]);
    expect(t.endedBy).toBe(id267(17));
    expect(t.reason).toBe("review still not approved after 2 revise attempt(s)");
    expect(t.failed).toBe(true);
  });

  test("try 2 is the rereview resume", () => {
    const t = tries[1];
    expect(t.n).toBe(2);
    expect(t.how).toBe("rereview");
    expect(t.who).toBe("Alex Doe");
    expect(t.start).toBe(at267("05:35:58.548"));
    expect(t.own).toEqual(ids267(18, 30));
    expect(t.reused).toEqual(ids267(1, 9));
    expect(t.endedBy).toBeNull();
    expect(t.reason).toBeNull();
    expect(t.failed).toBe(false);
  });

  test("without the decision event try 2 reads resume", () => {
    const bare = events.filter((e) => e.name !== "rereview_decision");
    const t = runTries(phases, bare, run);
    expect(t[1].how).toBe("resume");
  });

  test("a queued phase lands in the last try", () => {
    const queued = phase({
      phase_id: "queued:31",
      run_id: RUN267,
      seq: 31,
      name: "deploy",
      stage: "Ship",
      status: "queued",
      started_at: null,
      ended_at: null,
    });
    const t = runTries([...phases, queued], events, run);
    expect(t[1].own).toContain("queued:31");
    expect(t[0].own).not.toContain("queued:31");
  });

  test("no events means one try owning every phase", () => {
    const t = runTries(phases, []);
    expect(t).toHaveLength(1);
    expect(t[0].own).toEqual(ids267(1, 30));
    expect(t[0].reused).toEqual([]);
  });

  test("a malformed payload never throws", () => {
    const bad = [
      ev({ type: "log", payload: "x", started_at: at267("05:35:58.548") }),
      ev({ type: "error", payload: "x", phase_id: id267(17) }),
      ev({ type: "log", payload: { replayed: true }, started_at: "nope" }),
    ];
    expect(() => runTries(phases, bad, run)).not.toThrow();
  });

  test("no phases gives one empty try", () => {
    const t = runTries([], []);
    expect(t).toHaveLength(1);
    expect(t[0].n).toBe(1);
    expect(t[0].own).toEqual([]);
  });
});

describe("runTries resumes", () => {
  function replay(iso: string, phase_id: string | null = null): FeedEvent {
    return ev({ type: "log", phase_id, payload: { replayed: true }, started_at: iso });
  }

  test("replay logs 2 s apart are one resume", () => {
    const phases = [
      phase({ phase_id: "a", seq: 1, started_at: at267("06:00:00.000") }),
      phase({ phase_id: "b", seq: 2, started_at: at267("06:10:05.000") }),
    ];
    const events = [
      replay(at267("06:10:00.000"), "a"),
      replay(at267("06:10:02.000"), "a"),
    ];
    expect(runTries(phases, events, null)).toHaveLength(2);
  });

  test("two replay clusters 30 s apart are three tries", () => {
    const phases = [
      phase({ phase_id: "a", seq: 1, started_at: at267("06:00:00.000") }),
      phase({ phase_id: "b", seq: 2, started_at: at267("06:10:05.000") }),
      phase({ phase_id: "c", seq: 3, started_at: at267("06:20:00.000") }),
    ];
    const events = [
      replay(at267("06:10:00.000"), "a"),
      replay(at267("06:10:02.000"), "a"),
      replay(at267("06:10:32.000"), "a"),
    ];
    expect(runTries(phases, events, null)).toHaveLength(3);
  });
});

// ── backbone ─────────────────────────────────────────────────────────────────

describe("backbone", () => {
  const { run, phases, events } = run267();
  const tree = backbone(phases, events, run);

  test("the spine is one card per planned step", () => {
    expect(tree.spine).toEqual([...ids267(1, 11), ...ids267(26, 30)]);
    expect(tree.spine).toHaveLength(16);
  });

  test("two branches hang under the review card", () => {
    expect(tree.branches).toHaveLength(2);
    expect(tree.branches[0].anchor).toBe(id267(11));
    expect(tree.branches[0].try).toBe(1);
    expect(tree.branches[0].runs).toEqual(ids267(12, 17));
    expect(tree.branches[0].key).toBe(id267(12));
    expect(tree.branches[1].anchor).toBe(id267(11));
    expect(tree.branches[1].try).toBe(2);
    expect(tree.branches[1].runs).toEqual(ids267(18, 25));
    expect(tree.branches[1].key).toBe(id267(18));
    const runs = tree.branches.reduce((n, b) => n + b.runs.length, 0);
    expect(runs).toBe(14);
  });

  test("only the branched spine card has a final", () => {
    expect(Object.keys(tree.final)).toEqual([id267(11)]);
    expect(tree.final[id267(11)]).toEqual({
      run: id267(25),
      tries: 2,
      count: 15,
    });
  });

  test("ord counts a step's runs inside its try", () => {
    const want: [number, number][] = [
      [13, 2],
      [16, 3],
      [14, 2],
      [17, 3],
      [18, 1],
      [21, 2],
      [24, 3],
      [19, 1],
      [22, 2],
      [25, 3],
    ];
    for (const [seq, k] of want) expect(tree.ord[id267(seq)]).toBe(k);
  });

  test("tryOf and branchOf map every folded run", () => {
    expect(tree.tryOf[id267(17)]).toBe(1);
    expect(tree.tryOf[id267(18)]).toBe(2);
    expect(tree.branchOf[id267(13)]).toBe(id267(12));
    expect(tree.branchOf[id267(25)]).toBe(id267(18));
    expect(tree.branchOf[id267(11)]).toBeUndefined();
  });

  test("the ended phase and the invalidated phases are kept", () => {
    expect([...tree.ended]).toEqual([id267(17)]);
    for (let seq = 10; seq <= 17; seq++) {
      expect(tree.invalidated.has(id267(seq))).toBe(true);
    }
  });

  test("no events leaves one try and one branch under review", () => {
    const bare = backbone(phases, [], run);
    expect(bare.branches).toHaveLength(1);
    expect(bare.branches[0].try).toBe(1);
    expect(bare.branches[0].anchor).toBe(id267(11));
    expect(bare.branches[0].runs).toEqual(ids267(12, 25));
  });

  test("a build retry hangs under its build card", () => {
    const ps = [
      phase({ phase_id: "b1", seq: 1, name: "build_1", status: "fail", stage: "Build" }),
      phase({
        phase_id: "b1r1",
        seq: 2,
        name: "build_1_retry1",
        status: "success",
        stage: "Build",
      }),
      phase({ phase_id: "v1", seq: 3, name: "verify_1", status: "success", stage: "Build" }),
    ];
    const t = backbone(ps);
    expect(t.branches).toHaveLength(1);
    expect(t.branches[0].anchor).toBe("b1");
    expect(t.branches[0].runs).toEqual(["b1r1"]);
  });

  test("all-unique names put every phase on the spine", () => {
    const ps = ["a", "b", "c"].map((id, i) =>
      phase({ phase_id: id, seq: i + 1, name: `step_${i + 1}` }),
    );
    const t = backbone(ps);
    expect(t.branches).toEqual([]);
    expect(t.spine).toEqual(["a", "b", "c"]);
  });
});

// ── cardsOf: the face on a resumed Run ──────────────────────────────────────

describe("cardsOf on a resumed Run", () => {
  const { run, phases, events, envelopes } = run267();
  const tree = backbone(phases, events, run);
  const cards = cardsOf(phases, [], events, 0, "Alex Doe", envelopes, tree);
  const card = (seq: number) => cards.find((c) => c.id === id267(seq))!;

  test("a branch run keeps its raw name and strips the step label", () => {
    const c = card(13);
    expect(c.label).toBe("verify_3");
    expect(c.name).toBe("verify_3_retry3");
    expect(c.ord).toBe(2);
  });

  test("the review card wears its final run's face", () => {
    const c = card(11);
    expect(c.time).toBe("approved in try 2");
    expect(c.note).toBe("2 tries");
    expect(c.ctx).toBeNull();
    expect(c.mark).toBe("success");
    expect(c.tone).toBe("approved");
    expect(c.ended).toBe(false);
    expect(c.status).toBe("success");
  });

  test("the try-ending review reads blocked and ended", () => {
    const c = card(17);
    expect(c.ended).toBe(true);
    expect(c.mark).toBe("fail");
    expect(c.verdict).toBe("blocked");
    expect(c.sub).toBe("1 blocking · 41s");
  });

  test("an approved branch review reads approved", () => {
    const c = card(25);
    expect(c.verdict).toBe("approved");
    expect(c.time).toBe("approved · 38s");
    expect(c.mark).toBe("success");
  });

  test("a spine card with no branch keeps its duration", () => {
    const c = card(10);
    expect(c.time).toBe("2m");
    expect(c.note).toBeNull();
  });

  test("a blocked first review colours its time", () => {
    const early = phases.filter((p) => (p.seq ?? 0) <= 11);
    const earlyCards = cardsOf(
      early,
      [],
      events,
      0,
      "Alex Doe",
      envelopes.filter((e) => e.phase_id === id267(11)),
      backbone(early, events, run),
    );
    const c = earlyCards.find((x) => x.id === id267(11))!;
    expect(c.time).toBe("2 blocking · 1m");
    expect(c.mark).toBe("blocked");
    expect(c.tone).toBe("blocked");
  });
});

// ── pillFaces: one per try branch ───────────────────────────────────────────

describe("pillFaces", () => {
  const { run, phases, events, envelopes } = run267();
  const tree = backbone(phases, events, run);
  const cards = cardsOf(phases, [], events, 0, "Alex Doe", envelopes, tree);
  const faces = pillFaces(tree, cards);
  const keyOf = (tryN: number) => tree.branches.find((b) => b.try === tryN)!.key;

  test("try 1 reads not approved, two rounds, six runs", () => {
    const f = faces[keyOf(1)];
    expect(f.title).toBe("Try 1");
    expect(f.flag).toBeNull();
    expect(f.size).toBe("2 rounds");
    expect(f.verdict).toBe("✕ not approved");
    expect(f.ok).toBe(false);
    expect(f.tip).toContain("6 runs");
    expect(f.tip).toContain(
      "ended: review still not approved after 2 revise attempt(s)",
    );
  });

  test("try 2 reads rereview, approved, eight runs", () => {
    const f = faces[keyOf(2)];
    expect(f.flag).toBe("rereview");
    expect(f.size).toBe("2 rounds");
    expect(f.verdict).toBe("✓ approved");
    expect(f.ok).toBe(true);
    expect(f.tip).toContain("--rereview by Alex Doe");
    expect(f.tip).toContain("re-entered at verify_3");
    expect(f.tip).toContain("8 runs");
  });

  test("the pill time is the try start's local HH:MM", () => {
    const start = tree.tries[1].start!;
    const d = new Date(Date.parse(start));
    const hhmm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    expect(faces[keyOf(2)].time).toBe(hhmm);
  });
});

// ── the fold rules ──────────────────────────────────────────────────────────

describe("toggleTry / tryToOpen", () => {
  const { run, phases, events } = run267();
  const tree = backbone(phases, events, run);

  test("opens a try", () => {
    const r = toggleTry(new Set(), 2, null, tree);
    expect([...r.open]).toEqual([2]);
    expect(r.clear).toBe(false);
  });

  test("folding a try drops a selection inside it", () => {
    const r = toggleTry(new Set([2]), 2, id267(21), tree);
    expect([...r.open]).toEqual([]);
    expect(r.clear).toBe(true);
  });

  test("folding a try keeps a spine selection", () => {
    const r = toggleTry(new Set([2]), 2, id267(11), tree);
    expect([...r.open]).toEqual([]);
    expect(r.clear).toBe(false);
  });

  test("tryToOpen finds the branch's try", () => {
    expect(tryToOpen(id267(21), tree)).toBe(2);
    expect(tryToOpen(id267(11), tree)).toBeNull();
    expect(tryToOpen(null, tree)).toBeNull();
  });
});

// ── layoutCanvas: the backbone layout ────────────────────────────────────────

describe("layoutCanvas without repeats", () => {
  // Run 267's 16 spine phases, renamed s1…s16 so every step has a unique name.
  const phases: Phase[] = ROWS267.filter(
    ([seq]) => seq <= 11 || seq >= 26,
  ).map(([, name, kind, owner, stage], i) =>
    phase({
      phase_id: `s${i + 1}`,
      seq: i + 1,
      name,
      kind,
      owner,
      stage,
      status: "success",
    }),
  );
  const layout = layoutCanvas(phases);

  test("the size and bounds match today's output", () => {
    expect(layout.width).toBe(1756);
    expect(layout.height).toBe(778);
    expect(layout.bounds).toEqual({ x: 38, y: 28, w: 1680, h: 694 });
  });

  test("one label per stage, at the top of its first row", () => {
    expect(layout.labels.map((l) => l.text)).toEqual([
      "Prepare",
      "Build",
      "Review",
      "Ship",
    ]);
    expect(layout.labels.map((l) => l.y)).toEqual([32, 222, 412, 602]);
    expect(layout.labels.every((l) => l.x === 74)).toBe(true);
  });

  test("the spine sits at today's coordinates", () => {
    const at = (id: string) => layout.pos[id];
    const xs = [72, 350, 628, 906];
    xs.forEach((x, i) => {
      expect(at(`s${i + 1}`)).toEqual({ x, y: 58, row: 0, w: CARD_W, h: CARD_H, mini: false });
    });
    for (let i = 0; i < 6; i++) {
      expect(at(`s${i + 5}`)).toEqual({
        x: 72 + i * 278,
        y: 248,
        row: 1,
        w: CARD_W,
        h: CARD_H,
        mini: false,
      });
    }
    [72, 350, 628].forEach((x, i) => {
      expect(at(`s${i + 11}`)).toEqual({ x, y: 438, row: 2, w: CARD_W, h: CARD_H, mini: false });
      expect(at(`s${i + 14}`)).toEqual({ x, y: 628, row: 3, w: CARD_W, h: CARD_H, mini: false });
    });
  });

  test("every card is a full-size spine card", () => {
    for (const p of Object.values(layout.pos)) {
      expect(p.w).toBe(CARD_W);
      expect(p.h).toBe(CARD_H);
      expect(p.mini).toBe(false);
    }
  });

  test("15 spine edges, none a trunk", () => {
    expect(layout.edges).toHaveLength(15);
    expect(layout.edges.map((e) => e.id)).toEqual(
      Array.from({ length: 15 }, (_, i) => `s${i + 2}`),
    );
    expect(layout.edges.every((e) => !e.trunk)).toBe(true);
  });

  test("the wrap and the straight run keep today's path", () => {
    expect(layout.edges[3].d).toBe(
      "M1134 101 H1146 Q1158 101 1158 113 V166 Q1158 178 1146 178 H54 Q42 178 42 190 V279 Q42 291 54 291 H63",
    );
    expect(layout.edges[4].d).toBe("M300 291H341");
  });

  test("no repeats means no pills", () => {
    expect(layout.pills).toEqual([]);
  });
});

describe("layoutCanvas on a resumed Run", () => {
  const { run, phases, events } = run267();
  const tree = backbone(phases, events, run);

  test("folded: two pills, one trunk, today's width", () => {
    const layout = layoutCanvas(phases, tree);
    expect(Object.keys(layout.pos)).toHaveLength(16);
    expect(layout.pills).toHaveLength(2);
    expect(layout.pills[0]).toMatchObject({ try: 1, x: 106, y: 571, open: false });
    expect(layout.pills[1]).toMatchObject({ try: 2, x: 526, y: 571, open: false });
    expect(layout.pos[id267(11)].y).toBe(438);
    expect(layout.pos[id267(28)].y).toBe(688);
    expect(layout.edges.filter((e) => e.trunk)).toHaveLength(1);
    expect(layout.edges.filter((e) => !e.trunk)).toHaveLength(15);
    expect(layout.width).toBe(1756);
  });

  test("folded: the edge into Ship routes right of the pills", () => {
    const layout = layoutCanvas(phases, tree);
    const edge = layout.edges.find((e) => e.id === id267(28))!;
    expect(edge.d.startsWith("M856 481 H944 Q956 481")).toBe(true);
  });

  test("open both tries: every branch run gets a mini card", () => {
    const layout = layoutCanvas(phases, tree, new Set([1, 2]));
    expect(Object.keys(layout.pos)).toHaveLength(30);
    const minis = Object.values(layout.pos).filter((p) => p.mini);
    expect(minis).toHaveLength(14);
    expect(minis.every((p) => p.w === 168 && p.h === 58)).toBe(true);
    expect(layout.pos[id267(12)]).toMatchObject({ x: 116, y: 766 });
    expect(layout.pos[id267(18)]).toMatchObject({ x: 536, y: 606 });
    expect(layout.pills.every((p) => p.open)).toBe(true);
  });

  test("open both tries: columns align by round and push the rows down", () => {
    const layout = layoutCanvas(phases, tree, new Set([1, 2]));
    expect(layout.pos[id267(12)].y).toBe(layout.pos[id267(20)].y);
    expect(layout.pos[id267(17)].y).toBe(1166);
    expect(layout.pos[id267(25)].y).toBe(1166);
    expect(layout.pos[id267(28)].y).toBe(1328);
    expect(layout.height).toBe(1478);
  });

  test("open both tries: the ended run's column edge is broken", () => {
    const layout = layoutCanvas(phases, tree, new Set([1, 2]));
    expect(layout.edges.find((e) => e.id === id267(17))!.broken).toBe(true);
  });

  test("open only try 2: its column starts at the pill", () => {
    const layout = layoutCanvas(phases, tree, new Set([2]));
    expect(layout.pos[id267(18)].y).toBe(606);
    expect(layout.pos[id267(12)]).toBeUndefined();
  });

  test("open only try 1: its column starts at the pill", () => {
    const layout = layoutCanvas(phases, tree, new Set([1]));
    expect(layout.pos[id267(12)].y).toBe(606);
  });
});
