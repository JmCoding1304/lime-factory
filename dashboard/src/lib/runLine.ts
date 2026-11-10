import type {
  EventRow,
  FeedEvent,
  Phase,
  Run,
  RunDetail,
} from "./types";
import { cashDetail, cashWord, runCash } from "./cash";
import { fmtDuration, fmtK } from "./format";
import { fmtElapsed } from "./notch";
import { runName } from "./runsSheet";
import { runSignals, type RunSignal } from "./signals";
import {
  rescopeNotice,
  runLineTint,
  statusTone,
  statusWord,
  type LineTint,
  type RescopeNotice,
  type StatusTone,
} from "./status";
import { runError } from "./failure";

/**
 * The live feed's events in the row shape that `runCash`, `runSignals`,
 * `runError` and `rescopeNotice` read. The feed links events by `seq`
 * (`parent_seq`); the row shape links them by `event_id` (`parent_id`), so the
 * adapter maps `seq` onto both and re-serializes the parsed payload.
 */
export function feedRows(runId: string, events: FeedEvent[]): EventRow[] {
  return events.map((e) => ({
    rowid: e.seq,
    event_id: e.seq,
    run_id: runId,
    phase_id: e.phase_id,
    parent_id: e.parent_seq,
    type: e.type,
    name: e.name,
    payload_json: e.payload == null ? null : JSON.stringify(e.payload),
    tokens: e.tokens,
    started_at: e.started_at,
    ended_at: null,
  }));
}

/** The Run's elapsed time: ticking `m:ss`/`h:mm:ss` while open, the finished
 *  form (`42m`) once `ended_at` is valid, and `—` with no `started_at`. */
export function runElapsed(run: Run, nowMs: number): string {
  const endT = run.ended_at ? Date.parse(run.ended_at) : NaN;
  if (Number.isFinite(endT)) {
    return fmtDuration(run.started_at, run.ended_at, nowMs);
  }
  return fmtElapsed(run.started_at, nowMs);
}

/** The ticket-number segment: `runName`'s number, else the `run_id`. */
export function lineTicket(run: Run): string {
  return (
    runName({ ticket: run.ticket, request: null, run_id: run.run_id }).num ??
    run.run_id
  );
}

/** The status word, first letter capitalised; null/empty → `Unknown`. Unknown
 *  statuses pass through (`mystery` → `Mystery`). */
export function lineWord(status: string | null | undefined): string {
  const word = statusWord(status);
  if (!word) return "Unknown";
  return word[0].toUpperCase() + word.slice(1);
}

/** The `budget_warn` signal's count, else 0. */
export function warnCount(events: EventRow[]): number {
  return runSignals(events).find((s) => s.type === "budget_warn")?.count ?? 0;
}

export interface RunLineView {
  dot: StatusTone;
  tint: LineTint;
  word: string;
  warns: number;
  label: string;
  text: string;
}

/** The Run line: status · ticket · elapsed · cash, plus `▲N` with warnings. */
export function runLine(
  detail: RunDetail,
  events: EventRow[],
  nowMs: number,
): RunLineView {
  const run = detail.run;
  const warns = warnCount(events);
  const segments = [
    lineWord(run.status),
    lineTicket(run),
    runElapsed(run, nowMs),
    cashWord(runCash(detail.sessions, events)),
  ];
  if (warns > 0) segments.push(`▲${warns}`);
  const label = segments.join(" · ");
  return {
    dot: statusTone(run.status),
    tint: runLineTint(run.status, warns),
    word: lineWord(run.status),
    warns,
    label,
    text: "● " + label,
  };
}

export interface FailureLine {
  phaseId: string | null;
  text: string;
}

/** The Run tab's failure banner: the first failed phase with an error, else
 *  `runError`'s event-sourced fallback, else null. */
export function failureLine(
  phases: Phase[],
  events: EventRow[],
): FailureLine | null {
  for (const p of phases) {
    if (p.status === "fail" && p.error && p.error.trim() !== "") {
      return {
        phaseId: p.phase_id,
        text: `${p.name ?? p.phase_id} failed · ${p.error.trim()}`,
      };
    }
  }
  const text = runError([], events);
  if (text) return { phaseId: null, text: `failed · ${text}` };
  return null;
}

export interface RunFact {
  key: string;
  text: string;
  title?: string;
}

export interface RunInfoView {
  id: string;
  title: string;
  request: string;
  status: string;
  facts: RunFact[];
  signals: RunSignal[];
  notice: RescopeNotice | null;
  /** The phase the rescope notice points at, for its "Open <phase>" button. */
  noticePhase: { id: string; name: string } | null;
  failure: FailureLine | null;
}

/** The Run tab's body: identity, title, facts, chips and its one banner. */
export function runInfo(
  detail: RunDetail,
  events: EventRow[],
  nowMs: number,
): RunInfoView {
  const run = detail.run;
  const signals = runSignals(events);
  const warns = warnCount(events);
  const budget = signals.find((s) => s.type === "budget_warn") ?? null;
  const notice = rescopeNotice(run.status, events, run.worktree);
  const name = runName(run);

  // The notice's phase: the last `needs_rescope` event that names one.
  let noticePhase: { id: string; name: string } | null = null;
  if (notice) {
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i];
      if (e.name !== "needs_rescope" || !e.phase_id) continue;
      const p = detail.phases.find((x) => x.phase_id === e.phase_id);
      noticePhase = { id: e.phase_id, name: p?.name ?? e.phase_id };
      break;
    }
  }
  const live = !Number.isFinite(run.ended_at ? Date.parse(run.ended_at) : NaN);

  const facts: RunFact[] = [];
  if (run.project) facts.push({ key: "project", text: run.project });
  if (run.workflow) facts.push({ key: "workflow", text: run.workflow });
  if (run.started_at) {
    const elapsed = runElapsed(run, nowMs);
    facts.push({
      key: "elapsed",
      text: live ? `${elapsed} elapsed` : `took ${elapsed}`,
    });
  }
  if (warns > 0) {
    facts.push({
      key: "warns",
      text: `▲ ${warns} ${warns === 1 ? "warn" : "warns"}`,
      title: budget?.title,
    });
  }
  const cost = cashDetail(runCash(detail.sessions, events), run.total_cost, live);
  facts.push({ key: "cost", text: cost.text, title: cost.title });
  if (run.branch) facts.push({ key: "branch", text: run.branch });
  if (run.port != null) facts.push({ key: "port", text: `:${run.port}` });
  if (run.db_name) facts.push({ key: "db", text: run.db_name });
  if (run.profile) facts.push({ key: "profile", text: run.profile });
  if (run.worktree) facts.push({ key: "worktree", text: run.worktree });
  if (run.engineer) facts.push({ key: "engineer", text: run.engineer });
  if (run.total_tokens != null && run.total_tokens > 0) {
    facts.push({ key: "tokens", text: `${fmtK(run.total_tokens)} tokens` });
  }

  return {
    id: run.run_id,
    title: name.num ? `${name.num} ${name.text}` : name.text,
    request: run.request ?? "",
    status: run.status ?? "unknown",
    facts,
    signals: signals.filter((s) => s.type !== "budget_warn"),
    notice,
    noticePhase,
    failure: notice ? null : failureLine(detail.phases, events),
  };
}
