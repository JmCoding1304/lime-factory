import type { EventRow, Phase } from "./types";
import { parsePayload } from "./events";

/**
 * Regex matching the _retryN suffix the feature workflow appends on retry
 * attempts (lime/workflows/phases.py:62). Capture group 1 is the attempt
 * number (0-based: _retry0 means attempt 2, _retry1 means attempt 3, etc.).
 */
export const RETRY_NAME = /_retry(\d+)$/;

/**
 * The attempt number a phase represents; 1 means first try.
 *
 * Reads three signals and takes the strongest (max):
 *  - p.attempt ??? 1
 *  - (p.retries ?? 0) + 1
 *  - Number(m[1]) + 1 when the phase name ends in _retryN
 *
 * Clamped to >= 1 so hand-edited zero or negative values still yield 1.
 * The regex capture is guarded with Number.isFinite to avoid NaN.
 */
export function phaseAttempt(p: Phase): number {
  let a = p.attempt ?? 1;
  let r = (p.retries ?? 0) + 1;
  const m = RETRY_NAME.exec(p.name ?? "");
  if (m) {
    const n = Number(m[1]);
    if (Number.isFinite(n)) {
      a = Math.max(a, n + 1);
    }
  }
  return Math.max(a, r, 1);
}

/**
 * The step a phase belongs to: its name with the final `_retryN` suffix
 * stripped. A null name falls back to the phase_id. Only the last suffix is
 * removed, so `x_retry1_retry2` reads `x_retry1`.
 */
export function stepName(p: Pick<Phase, "name" | "phase_id">): string {
  return (p.name ?? p.phase_id).replace(RETRY_NAME, "");
}

/**
 * The phase's own error, trimmed, but only when the phase actually failed and
 * the text is non-blank. Otherwise null — a green phase never carries one.
 */
export function phaseError(p: Phase): string | null {
  if (p.status !== "fail") return null;
  const err = p.error?.trim();
  return err ? err : null;
}

/**
 * The human-readable error that killed the run, assembled from two sources:
 *
 * 1. The FIRST phase (input order, already seq-sorted by RunTrace) with
 *    status === "fail" AND a non-blank error → return that error trimmed.
 * 2. Failing that, the LAST event with type === "error" whose parsed payload
 *    contains a non-blank string "error" or (fallback) "reason" field.
 * 3. Otherwise null.
 *
 * Never returns blank text — empty/whitespace-only → null.
 * Never reads error off a phase whose status is not "fail".
 */
export function runError(
  phases: Phase[],
  events: EventRow[],
): string | null {
  // First failing phase with a non-blank error
  for (const p of phases) {
    const err = phaseError(p);
    if (err !== null) return err;
  }

  // Last error event with a non-blank error/reason field
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type !== "error") continue;
    const payload = parsePayload(e.payload_json);
    if (!payload) continue;
    const err = payload.error;
    if (typeof err === "string" && err.trim() !== "") {
      return err.trim();
    }
    const reason = payload.reason;
    if (typeof reason === "string" && reason.trim() !== "") {
      return reason.trim();
    }
  }

  return null;
}