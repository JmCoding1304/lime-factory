import type { EventRow } from "./types";
import { parsePayload } from "./events";

/**
 * Ledger reads status as a word, not an icon (prototype 18). This is the one
 * place the words and their colour family live, shared by the run rows, the
 * run/node pills and the run header so every status renders identically.
 *
 * Every existing word is unchanged. `needs_rescope` (ticket 131) is a distinct
 * warning word: a preserved run awaiting an engineer's explicit decision — not
 * a failure and never a success.
 */
export const STATUS_WORD: Record<string, string> = {
  running: "running",
  success: "success",
  fail: "failed",
  budget_exhausted: "budget",
  needs_rescope: "needs rescope",
  deployed: "deployed",
  discarded: "discarded",
  queued: "queued",
};

export type StatusTone = "run" | "ok" | "bad" | "warn" | "neutral";

/** The colour family a status earns. needs_rescope is a warning — not the
 *  failure red and not the success green. */
export function statusTone(status: string | null | undefined): StatusTone {
  switch (status) {
    case "running":
      return "run";
    case "success":
    case "deployed":
      return "ok";
    case "fail":
    case "budget_exhausted":
      return "bad";
    case "needs_rescope":
      return "warn";
    default:
      return "neutral";
  }
}

/** The ledger word for a status; unknown statuses pass through unchanged. */
export function statusWord(status: string | null | undefined): string {
  if (!status) return "";
  return STATUS_WORD[status] ?? status;
}

/** The Run line's colour family, a coarser read than `StatusTone`. */
export type LineTint = "bad" | "warn" | "quiet";

/**
 * The Run line's tint. Red on a failed Run (`fail`, `budget_exhausted`); amber
 * on a rescope or a budget warning; red outranks amber. Every other status,
 * including unknown and null, is quiet.
 */
export function runLineTint(
  status: string | null | undefined,
  warns: number,
): LineTint {
  const tone = statusTone(status);
  if (tone === "bad") return "bad";
  if (tone === "warn" || warns > 0) return "warn";
  return "quiet";
}

// ── needs_rescope notice ─────────────────────────────────────────────────────

/** The latest `handoff`/`needs_rescope` event in the stream, payload aside. */
function latestRescopeHandoff(events: EventRow[]): EventRow | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === "handoff" && e.name === "needs_rescope") return e;
  }
  return null;
}

/** The recorded engineer decision text from the latest decision event bound to
 *  this handoff event. A decision for an earlier overflow is not reported. The
 *  dashboard is read-only: it reports what was recorded, never approves. */
function latestRecoveryDecision(
  events: EventRow[],
  handoffEventId: number | null,
): string | null {
  if (handoffEventId === null) return null;
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type !== "log" || e.name !== "rescope_recovery_decision") continue;
    if (e.parent_id !== handoffEventId) continue;
    const payload = parsePayload(e.payload_json);
    const decision = payload?.decision;
    if (typeof decision === "string" && decision.trim() !== "") {
      return decision.trim();
    }
  }
  return null;
}

export interface RescopeNotice {
  /** The ledger word for the outcome. */
  word: string;
  /** The latest handoff artifact path, or null when the event/payload is
   *  missing or malformed — reported as unavailable, never invented. */
  handoffPath: string | null;
  /** The latest handoff event's id, or null when no handoff event is present. */
  eventId: number | null;
  /** The preserved worktree, or null when the run carries none. */
  worktree: string | null;
  /** The recorded engineer decision, when one was recorded. */
  decision: string | null;
  /** True until an engineer's decision has been recorded for this handoff. */
  decisionRequired: boolean;
  /** A preserved run is never deployable as-is. */
  deployable: boolean;
  /** The banner headline. */
  headline: string;
  /** Detail lines, ready to render — fallback text where data is missing. */
  lines: string[];
}

/**
 * The rescope-specific warning a needs_rescope run earns, derived purely from
 * the run's outcome and its already-fetched event stream — a run whose phases
 * all succeeded still gets it. Returns null for every other status, so fail
 * and success keep their ordinary presentation.
 */
export function rescopeNotice(
  status: string | null | undefined,
  events: EventRow[],
  worktree?: string | null,
): RescopeNotice | null {
  if (status !== "needs_rescope") return null;

  const handoff = latestRescopeHandoff(events);
  const payload = handoff ? parsePayload(handoff.payload_json) : null;
  const path = payload?.path;
  const handoffPath =
    typeof path === "string" && path.trim() !== "" ? path.trim() : null;
  const eventId = handoff?.event_id ?? null;
  const decision = latestRecoveryDecision(events, eventId);
  const preservedWorktree = worktree ? worktree : null;

  const lines = [
    handoffPath !== null
      ? `handoff: ${handoffPath}${eventId !== null ? ` (event ${eventId})` : ""}`
      : "handoff: unavailable — no readable handoff path/event recorded",
    preservedWorktree !== null
      ? `preserved worktree: ${preservedWorktree}`
      : "preserved worktree: unavailable",
    decision !== null
      ? `recorded engineer decision: ${decision}`
      : "engineer decision required — no automatic recovery, and this run is not deployable",
  ];

  return {
    word: statusWord("needs_rescope"),
    handoffPath,
    eventId,
    worktree: preservedWorktree,
    decision,
    decisionRequired: decision === null,
    deployable: false,
    headline: "needs rescope — preserved; not accepted or deployable",
    lines,
  };
}