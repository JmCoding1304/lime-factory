import { describe, expect, test } from "bun:test";
import type { EventRow, Phase } from "./types";
import { phaseAttempt, runError, stepName } from "./failure";

// ── Helpers ──────────────────────────────────────────────────────────────────

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

// ── phaseAttempt ─────────────────────────────────────────────────────────────

describe("phaseAttempt", () => {
  test("a clean first-try phase is attempt 1", () => {
    expect(phaseAttempt(phase({}))).toBe(1);
  });

  test("reads the attempt column", () => {
    expect(phaseAttempt(phase({ attempt: 3 }))).toBe(3);
  });

  test("reads the retries column when attempt lags", () => {
    expect(phaseAttempt(phase({ attempt: 1, retries: 2 }))).toBe(3);
  });

  test("reads the _retryN name suffix the feature workflow writes", () => {
    expect(phaseAttempt(phase({ name: "build_1_retry1" }))).toBe(2);
    expect(phaseAttempt(phase({ name: "verify_1_retry2" }))).toBe(3);
  });

  test("takes the strongest signal when they disagree", () => {
    expect(phaseAttempt(phase({ name: "build_1_retry2", attempt: 1 }))).toBe(3);
  });

  test("a plain name that merely contains retry is not a retry", () => {
    expect(phaseAttempt(phase({ name: "retry_budget" }))).toBe(1);
  });

  test("null name and null columns fall back to 1", () => {
    expect(
      phaseAttempt(phase({ name: null, attempt: null, retries: null })),
    ).toBe(1);
  });

  test("never returns less than 1", () => {
    expect(phaseAttempt(phase({ attempt: 0 }))).toBe(1);
    expect(phaseAttempt(phase({ attempt: -3 }))).toBe(1);
  });
});

// ── stepName ─────────────────────────────────────────────────────────────────

describe("stepName", () => {
  test("strips the final _retryN suffix", () => {
    expect(stepName({ name: "verify_3_retry3", phase_id: "p" })).toBe("verify_3");
    expect(stepName({ name: "review_retry2", phase_id: "p" })).toBe("review");
  });

  test("a first-try name keeps its step number", () => {
    expect(stepName({ name: "build_1", phase_id: "p" })).toBe("build_1");
  });

  test("a name that merely contains retry is unchanged", () => {
    expect(stepName({ name: "retry_budget", phase_id: "p" })).toBe(
      "retry_budget",
    );
  });

  test("only the last suffix is stripped", () => {
    expect(stepName({ name: "x_retry1_retry2", phase_id: "p" })).toBe(
      "x_retry1",
    );
  });

  test("a null name falls back to the phase_id", () => {
    expect(stepName({ name: null, phase_id: "2b70e1b9:05" })).toBe(
      "2b70e1b9:05",
    );
  });
});

// ── runError ─────────────────────────────────────────────────────────────────

describe("runError", () => {
  test("a green run has no error", () => {
    expect(
      runError(
        [phase({ status: "success" }), phase({ status: "success" })],
        [],
      ),
    ).toBeNull();
  });

  test("surfaces the failing phase's error", () => {
    expect(
      runError(
        [
          phase({ status: "success", seq: 1 }),
          phase({ status: "fail", error: "boom", seq: 2 }),
        ],
        [],
      ),
    ).toBe("boom");
  });

  test("ignores sticky error text on a phase that succeeded", () => {
    expect(
      runError(
        [phase({ status: "success", error: "stale failure from attempt 1" })],
        [],
      ),
    ).toBeNull();
  });

  test("prefers the first failing phase when several failed", () => {
    expect(
      runError(
        [
          phase({
            status: "fail",
            error: "nothing to commit — the preceding phases changed no files",
            seq: 1,
          }),
          phase({
            status: "fail",
            error: "git push … rejected",
            seq: 2,
          }),
        ],
        [],
      ),
    ).toBe("nothing to commit — the preceding phases changed no files");
  });

  test("falls back to the run's error event when no phase failed", () => {
    expect(
      runError(
        [phase({ status: "success" })],
        [
          ev({
            type: "error",
            payload_json:
              '{"reason":"step 1 still failed verify after 3 attempts"}',
          }),
        ],
      ),
    ).toBe("step 1 still failed verify after 3 attempts");
  });

  test("reads an error event's error key too", () => {
    expect(
      runError(
        [phase({ status: "success" })],
        [ev({ type: "error", payload_json: '{"error":"boom"}' })],
      ),
    ).toBe("boom");
  });

  test("takes the last error event when several were recorded", () => {
    expect(
      runError(
        [phase({ status: "success" })],
        [
          ev({ rowid: 1, type: "error", payload_json: '{"error":"first"}' }),
          ev({ rowid: 2, type: "error", payload_json: '{"error":"last"}' }),
        ],
      ),
    ).toBe("last");
  });

  test("a failing phase beats an error event", () => {
    expect(
      runError(
        [
          phase({
            status: "fail",
            error: "phase error",
            seq: 1,
          }),
        ],
        [ev({ type: "error", payload_json: '{"error":"event error"}' })],
      ),
    ).toBe("phase error");
  });

  test("a budget_exhausted run with no failing phase and no error event yields null", () => {
    expect(
      runError(
        [phase({ status: "success" })],
        [ev({ type: "budget_warn" })],
      ),
    ).toBeNull();
  });

  test("blank and whitespace-only error text is treated as absent", () => {
    expect(
      runError(
        [phase({ status: "fail", error: "   " })],
        [],
      ),
    ).toBeNull();
  });

  test("malformed event payloads do not throw", () => {
    expect(() =>
      runError(
        [phase({ status: "success" })],
        [
          ev({ type: "error", payload_json: "not json" }),
          ev({ type: "error", payload_json: "null" }),
          ev({ type: "error", payload_json: "[1,2]" }),
        ],
      ),
    ).not.toThrow();
    // Also assert result is null since none of those payloads yield a string field
    expect(
      runError(
        [phase({ status: "success" })],
        [
          ev({ type: "error", payload_json: "not json" }),
          ev({ type: "error", payload_json: "null" }),
          ev({ type: "error", payload_json: "[1,2]" }),
        ],
      ),
    ).toBeNull();
  });

  test("a non-string reason is ignored", () => {
    expect(
      runError(
        [phase({ status: "success" })],
        [ev({ type: "error", payload_json: '{"reason": 42}' })],
      ),
    ).toBeNull();
  });
});