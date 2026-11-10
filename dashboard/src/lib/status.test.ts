import { describe, expect, test } from "bun:test";
import type { EventRow } from "./types";
import {
  STATUS_WORD,
  rescopeNotice,
  statusTone,
  statusWord,
} from "./status";

// ── Helpers ─────────────────────────────────────────────────────────────────

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

function handoff(eventId: number, path: string): EventRow {
  return ev({
    event_id: eventId,
    type: "handoff",
    name: "needs_rescope",
    payload_json: JSON.stringify({ path }),
  });
}

function decision(eventId: number, parentId: number, text: string): EventRow {
  return ev({
    event_id: eventId,
    parent_id: parentId,
    type: "log",
    name: "rescope_recovery_decision",
    payload_json: JSON.stringify({
      action: "rescope_and_resume",
      decision: text,
      engineer: "dev",
      handoff_path: "/tmp/handoff.json",
    }),
  });
}

// ── words & tones ────────────────────────────────────────────────────────────

describe("status presentation", () => {
  test("needs_rescope has a distinct warning word", () => {
    expect(statusWord("needs_rescope")).toBe("needs rescope");
    expect(statusTone("needs_rescope")).toBe("warn");
    // Distinct from the failure presentation it must never borrow.
    expect(statusWord("needs_rescope")).not.toBe(statusWord("fail"));
    expect(statusTone("needs_rescope")).not.toBe(statusTone("fail"));
    expect(statusTone("needs_rescope")).not.toBe(statusTone("success"));
    // Existing words are untouched.
    expect(STATUS_WORD.fail).toBe("failed");
    expect(statusWord("budget_exhausted")).toBe("budget");
  });

  test("fail and success retain their ordinary presentation", () => {
    expect(statusWord("success")).toBe("success");
    expect(statusWord("fail")).toBe("failed");
    expect(statusWord("deployed")).toBe("deployed");
    expect(statusTone("success")).toBe("ok");
    expect(statusTone("deployed")).toBe("ok");
    expect(statusTone("fail")).toBe("bad");
    expect(statusTone("budget_exhausted")).toBe("bad");
    expect(statusTone("running")).toBe("run");
    // No rescope notice is derived for ordinary outcomes.
    expect(rescopeNotice("fail", [handoff(1, "/t/h.json")], "/wt")).toBeNull();
    expect(rescopeNotice("success", [], null)).toBeNull();
    expect(rescopeNotice("discarded", [], null)).toBeNull();
  });
});

// ── rescope notice ───────────────────────────────────────────────────────────

describe("rescope notice", () => {
  test("rescope notice uses the latest handoff event", () => {
    const events = [
      handoff(2, "/tmp/old-handoff.json"),
      decision(3, 2, "earlier approval"),
      handoff(9, "/tmp/current-handoff.json"),
    ];
    const notice = rescopeNotice("needs_rescope", events, "/wt/feature-131");
    expect(notice).not.toBeNull();
    expect(notice!.handoffPath).toBe("/tmp/current-handoff.json");
    expect(notice!.eventId).toBe(9);
    expect(notice!.worktree).toBe("/wt/feature-131");
    expect(notice!.headline).toContain("preserved");
    expect(notice!.headline).toContain("not accepted or deployable");
    expect(notice!.decisionRequired).toBe(true);
    expect(notice!.deployable).toBe(false);
    // Notice is derived from outcome + events, never phase rows — it survives
    // a run whose phases all succeeded.
    expect(notice!.lines.join("\n")).toContain("/tmp/current-handoff.json");
    expect(notice!.lines.join("\n")).toContain("event 9");
  });

  test("a recorded decision is reported without approving anything", () => {
    const events = [handoff(9, "/tmp/current-handoff.json"), decision(10, 9, "scope cut to fit")];
    const notice = rescopeNotice("needs_rescope", events, "/wt");
    expect(notice!.decision).toBe("scope cut to fit");
    expect(notice!.decisionRequired).toBe(false);
    expect(notice!.deployable).toBe(false);
    expect(notice!.lines.join("\n")).toContain("scope cut to fit");
  });

  test("missing or malformed handoff remains an engineer-decision notice", () => {
    // No handoff event at all.
    const none = rescopeNotice("needs_rescope", [], "/wt");
    expect(none).not.toBeNull();
    expect(none!.handoffPath).toBeNull();
    expect(none!.eventId).toBeNull();
    expect(none!.decisionRequired).toBe(true);
    expect(none!.deployable).toBe(false);
    expect(none!.lines.join("\n")).toContain("unavailable");

    // A handoff event whose payload cannot be read: keep the event id, report
    // the path as unavailable rather than inventing one.
    const malformed = rescopeNotice(
      "needs_rescope",
      [ev({ event_id: 7, type: "handoff", name: "needs_rescope", payload_json: "{not json" })],
      "/wt",
    );
    expect(malformed!.handoffPath).toBeNull();
    expect(malformed!.eventId).toBe(7);
    expect(malformed!.decisionRequired).toBe(true);
    expect(malformed!.deployable).toBe(false);
    expect(malformed!.lines.join("\n")).toContain("unavailable");

    // A malformed decision payload yields no decision text.
    const badDecision = rescopeNotice(
      "needs_rescope",
      [
        handoff(9, "/tmp/h.json"),
        ev({
          event_id: 10,
          type: "log",
          name: "rescope_recovery_decision",
          payload_json: JSON.stringify({ decision: "   " }),
        }),
      ],
      null,
    );
    expect(badDecision!.decision).toBeNull();
    expect(badDecision!.worktree).toBeNull();
    expect(badDecision!.lines.join("\n")).toContain("engineer decision required");
  });
});