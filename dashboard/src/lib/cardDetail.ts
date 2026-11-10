import type {
  EventRow,
  Envelope,
  FeedEvent,
  FeedRun,
  GateResult,
  PhaseKind,
} from "./types";
import { phaseAttempt, phaseError } from "./failure";
import { statusTone, statusWord, type StatusTone } from "./status";
import { phaseSignals, type PhaseSignal } from "./signals";
import { phaseCost } from "./cash";
import { parsePayload } from "./events";
import { contextBar, liveAction } from "./canvas";
import { fmtClock, fmtDuration, fmtK, ts } from "./format";
import { modelName } from "./models";

// ── The detail's view model ──────────────────────────────────────────────────
//
// A pure module: no DOM and no state. Everything the CardDetail component
// renders is computed here, from the feed's `run` frame plus its events, so it
// can be exercised without a mount harness.

export interface DetailFact {
  key: string;
  label: string;
  text: string;
  title?: string;
  tone?: "bad";
}

export interface DiffFact {
  added: number;
  cap: number;
  over: boolean;
  text: string;
}

export interface Touched {
  count: number;
  label: string;
  paths: string[];
  diff: DiffFact | null;
}

export interface Artifact {
  /** The path exactly as the envelope declared it. */
  path: string;
  /** The repo-relative path to open, or null when it is outside the repo. */
  open: string | null;
}

export interface Outcome {
  type: string;
  valid: boolean;
  status: string | null;
  summary: string | null;
  notes: string | null;
  artifacts: Artifact[];
}

/** One item a gate inspected — the parsed element of `checks_json`. */
export interface CheckItem {
  item: string;
  ok: boolean;
  note: string;
}

export interface CheckRow {
  key: string;
  ok: boolean;
  name: string;
  attempt: number;
  violations: string[];
  /** The gate's per-item evidence; empty when the row carries none. */
  checks: CheckItem[];
  /** The count beside the name: `3 checks` on a pass, `2 other checks passed`
   *  on a failure whose evidence also lists passing items. Null with no
   *  evidence. */
  count: string | null;
}

export interface CardDetailView {
  id: string;
  name: string;
  kind: PhaseKind | null;
  word: string;
  tone: StatusTone;
  attempt: number;
  multi: boolean;
  description: string | null;
  facts: DetailFact[];
  context: { pct: number; hot: boolean } | null;
  signals: PhaseSignal[];
  error: string | null;
  outcome: Outcome | null;
  touched: Touched | null;
  checks: CheckRow[];
  request: string | null;
  brief: string | null;
  sessionId: string | null;
  /** The live line for a running agent phase, else null. */
  live: string | null;
}

// ── Small readers ────────────────────────────────────────────────────────────

/** Non-blank string after trimming, else null. */
function nonEmptyStr(v: unknown): string | null {
  if (typeof v !== "string") return null;
  const t = v.trim();
  return t === "" ? null : t;
}

/** The last `agent_start` event's payload, or null. */
function lastAgentStartPayload(events: EventRow[]): Record<string, unknown> | null {
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].type === "agent_start") return parsePayload(events[i].payload_json);
  }
  return null;
}

/** The last `turn` event's `tokens` when it is a number, else null. */
function lastTurnTokens(events: EventRow[]): number | null {
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].type === "turn") {
      const t = events[i].tokens;
      return typeof t === "number" ? t : null;
    }
  }
  return null;
}

// ── Diff fact ────────────────────────────────────────────────────────────────

/**
 * The Run's latest verify measurement, as a fact. Scan from the end for the
 * last `log` event named `verify_<digit>` whose payload carries integer
 * `total_added` and `max_added_lines`; a replayed verify (no numbers) is
 * skipped, so the earlier recorded measurement stays the fact. No such row is
 * null.
 */
export function diffFact(events: EventRow[]): DiffFact | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type !== "log") continue;
    if (!e.name || !/^verify_\d/.test(e.name)) continue;
    const p = parsePayload(e.payload_json);
    if (!p) continue;
    const added = p.total_added;
    const cap = p.max_added_lines;
    if (!Number.isInteger(added) || !Number.isInteger(cap)) continue;
    const a = added as number;
    const c = cap as number;
    const text =
      c > 0
        ? `+${a.toLocaleString("en-US")} of ${c.toLocaleString("en-US")} lines`
        : `+${a.toLocaleString("en-US")} lines`;
    return { added: a, cap: c, over: c > 0 && a > c, text };
  }
  return null;
}

// ── Files touched ────────────────────────────────────────────────────────────

/** One phase's `paths_touched` paths, first-seen order, deduped. */
export function touchedPaths(events: EventRow[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const e of events) {
    if (e.type !== "log" || e.name !== "paths_touched") continue;
    const paths = parsePayload(e.payload_json)?.paths;
    if (!Array.isArray(paths)) continue;
    for (const entry of paths) {
      if (typeof entry !== "string" || seen.has(entry)) continue;
      seen.add(entry);
      out.push(entry);
    }
  }
  return out;
}

// ── Outcome ──────────────────────────────────────────────────────────────────

/** The phase's envelope that carries the outcome: the latest valid one when
 *  one exists, else the latest. Sorted by `(attempt, envelope_id)`. */
function pickEnvelope(envelopes: Envelope[], phaseId: string): Envelope | null {
  const own = envelopes.filter((e) => e.phase_id === phaseId);
  if (own.length === 0) return null;
  const sorted = [...own].sort(
    (a, b) => (a.attempt ?? 0) - (b.attempt ?? 0) || a.envelope_id - b.envelope_id,
  );
  for (let i = sorted.length - 1; i >= 0; i--) {
    if (sorted[i].valid) return sorted[i];
  }
  return sorted[sorted.length - 1];
}

/** The phase's Outcome fact. A malformed `payload_json` gives null fields and
 *  never throws. No envelopes → null. */
export function phaseOutcome(envelopes: Envelope[], phaseId: string): Outcome | null {
  const env = pickEnvelope(envelopes, phaseId);
  if (!env) return null;
  const p = parsePayload(env.payload_json);
  const summary = nonEmptyStr(p?.summary);
  const notes = nonEmptyStr(p?.notes_for_next_agent);
  const status = typeof p?.status === "string" ? p.status : null;
  const artifacts = Array.isArray(p?.artifacts)
    ? p.artifacts
        .filter((x): x is string => typeof x === "string")
        .map((a): Artifact => ({ path: a, open: repoPath(a) }))
    : [];
  return {
    type: env.output_type ?? "Envelope",
    valid: !!env.valid,
    status,
    summary,
    notes,
    artifacts,
  };
}

// ── Checks ───────────────────────────────────────────────────────────────────

/**
 * The repo-relative form of an artifact path, or null when it cannot be opened
 * from the project's file tree: absolute paths, `~`, URLs, and any path that
 * climbs out of the repo with `..`. Agents report paths relative to the repo
 * root (lime/gates.py `_resolve`), so a relative path is the openable case.
 */
export function repoPath(artifact: string): string | null {
  const p = artifact.trim();
  if (p === "") return null;
  if (p.startsWith("/") || p.startsWith("~") || p.startsWith("\\")) return null;
  if (/^[a-zA-Z]:[\\/]/.test(p)) return null;
  if (p.includes("://")) return null;
  const parts = p.split(/[\\/]+/).filter((s) => s !== "" && s !== ".");
  if (parts.length === 0 || parts.includes("..")) return null;
  return parts.join("/");
}

/** A gate's per-item evidence: the parsed `checks_json` array. A malformed or
 *  non-array value gives an empty list, so the row falls back to its verdict. */
export function gateChecks(raw: string | null): CheckItem[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.map((v): CheckItem => {
      const o = (v ?? {}) as Record<string, unknown>;
      return {
        item: typeof o.item === "string" ? o.item : JSON.stringify(v),
        ok: !!o.ok,
        note: typeof o.note === "string" ? o.note : "",
      };
    });
  } catch {
    return [];
  }
}

/** A failed gate's violations: the JSON array mapped to strings; a malformed
 *  or non-array value gives `[raw]` when raw is non-empty. */
function gateViolations(raw: string | null): string[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      return parsed.map((v) => (typeof v === "string" ? v : JSON.stringify(v)));
    }
  } catch {
    /* legacy or truncated payloads render raw */
  }
  return [raw];
}

/** One gate row per gate, then one envelope row per envelope, stable-sorted by
 *  attempt so gates and envelopes stay interleaved by try. */
export function phaseChecks(
  gates: GateResult[],
  envelopes: Envelope[],
  phaseId: string,
): CheckRow[] {
  const gateRows = gates
    .filter((g) => g.phase_id === phaseId)
    .sort((a, b) => (a.attempt ?? 0) - (b.attempt ?? 0) || a.id - b.id)
    .map((g): CheckRow => {
      const ok = !!g.passed;
      const checks = gateChecks(g.checks_json);
      const passed = checks.filter((c) => c.ok).length;
      return {
        key: "g" + g.id,
        ok,
        name: g.gate ?? "gate",
        attempt: g.attempt ?? 1,
        violations: ok ? [] : gateViolations(g.violations_json),
        checks,
        count: ok
          ? checks.length
            ? `${checks.length} check${checks.length === 1 ? "" : "s"}`
            : null
          : passed
            ? `${passed} other check${passed === 1 ? "" : "s"} passed`
            : null,
      };
    });

  const envRows = envelopes
    .filter((e) => e.phase_id === phaseId)
    .sort(
      (a, b) => (a.attempt ?? 0) - (b.attempt ?? 0) || a.envelope_id - b.envelope_id,
    )
    .map((env): CheckRow => {
      const ok = !!env.valid;
      const raw = env.payload_json ?? "";
      return {
        key: "e" + env.envelope_id,
        ok,
        name: `envelope ${ok ? "valid" : "invalid"} — ${env.output_type ?? "Envelope"}`,
        attempt: env.attempt ?? 1,
        violations: ok ? [] : [raw.slice(0, 300) || "did not validate"],
        checks: [],
        count: null,
      };
    });

  return [...gateRows, ...envRows].sort((a, b) => a.attempt - b.attempt);
}

// ── The view ─────────────────────────────────────────────────────────────────

export function cardDetail(
  run: FeedRun,
  events: EventRow[],
  phaseId: string | null,
  nowMs: number,
): CardDetailView | null {
  if (!phaseId) return null;
  const p = run.phases.find((x) => x.phase_id === phaseId);
  if (!p) return null;

  const phaseEvents = events.filter((e) => e.phase_id === phaseId);
  const phaseSessions = run.sessions.filter((s) => s.phase_id === phaseId);
  const session = phaseSessions.at(-1) ?? null;

  const status = p.status ?? "queued";
  const attempt = phaseAttempt(p);
  const agent = p.kind === "agent";
  const desc = p.description?.trim();
  const name = p.name ?? p.phase_id;

  const facts: DetailFact[] = [];
  const add = (f: DetailFact): void => {
    if (f.text !== "") facts.push(f);
  };

  // 1. Role
  const roleLabel = agent ? "Role" : p.kind === "code" ? "Runner" : "Engineer";
  add({ key: "role", label: roleLabel, text: p.owner ?? "—" });

  let contextView: { pct: number; hot: boolean } | null = null;

  if (agent) {
    // 2. Model
    const modelId = session?.model ?? null;
    add({
      key: "model",
      label: "Model",
      text: modelName(modelId) || "—",
      title: modelId ?? undefined,
    });

    // 3. Runtime
    const thinking = nonEmptyStr(lastAgentStartPayload(phaseEvents)?.thinking);
    const runtimeText = [session?.runtime, thinking ? "thinking " + thinking : null]
      .filter((x): x is string => !!x)
      .join(" · ");
    add({ key: "runtime", label: "Runtime", text: runtimeText });
  }

  // 4. Time
  if (Number.isFinite(ts(p.started_at))) {
    const e = p.ended_at;
    const finished = e != null && Number.isFinite(ts(e)) && status !== "running";
    add({
      key: "time",
      label: "Time",
      text: finished
        ? `${fmtClock(p.started_at)} → ${fmtClock(e)} · ${fmtDuration(p.started_at, e, nowMs)}`
        : `since ${fmtClock(p.started_at)} · ${fmtDuration(p.started_at, null, nowMs)}`,
    });
  }

  if (agent) {
    // 5. Work
    const turns = phaseEvents.filter((e) => e.type === "turn").length;
    const calls = phaseEvents.filter((e) => e.type === "tool_call").length;
    add({
      key: "work",
      label: "Work",
      text:
        `${turns} turn${turns === 1 ? "" : "s"}` +
        ` · ${calls} tool call${calls === 1 ? "" : "s"}`,
    });

    // 6. Cost
    const cost = phaseCost(phaseSessions, phaseEvents);
    if (cost) add({ key: "cost", label: "Cost", text: cost.text, title: cost.title });

    // 7. Context
    const lastTokens = lastTurnTokens(phaseEvents);
    const ctxTokens = session?.context_tokens ?? null;
    const used =
      lastTokens !== null && lastTokens > 0
        ? lastTokens
        : ctxTokens !== null && ctxTokens > 0
          ? ctxTokens
          : null;
    if (used !== null) {
      const warnRaw = session?.budget_warn ?? null;
      const warn = warnRaw !== null && warnRaw > 0 ? warnRaw : null;
      add({
        key: "context",
        label: "Context",
        text: warn !== null ? `${fmtK(used)} / ${fmtK(warn)}` : `${fmtK(used)}`,
        title: `${used.toLocaleString("en-US")} tokens`,
      });
      const bar = contextBar(used, warn);
      contextView = bar ? { pct: bar.pct, hot: bar.hot } : null;
    }
  }

  // The live line: only a running agent phase has one.
  const live =
    status === "running" && p.kind === "agent"
      ? liveAction(phaseEvents.map(toFeedEvent))
      : null;

  const paths = touchedPaths(phaseEvents);
  const touched: Touched | null =
    paths.length === 0
      ? null
      : {
          count: paths.length,
          label: `${paths.length} ${paths.length === 1 ? "file" : "files"} touched`,
          paths,
          diff: diffFact(events),
        };

  return {
    id: p.phase_id,
    name,
    kind: p.kind,
    word: statusWord(status),
    tone: statusTone(status),
    attempt,
    multi: attempt > 1,
    description: desc ? desc : null,
    facts,
    context: contextView,
    signals: phaseSignals(phaseEvents),
    error: phaseError(p),
    outcome: phaseOutcome(run.envelopes, phaseId),
    touched,
    checks: phaseChecks(run.gates, run.envelopes, phaseId),
    request: lastPromptLog(phaseEvents),
    brief: lastPlanProvenance(phaseEvents),
    sessionId: session?.session_id ?? nonEmptyStr(lastAgentStartPayload(phaseEvents)?.session_id),
    live,
  };
}

/** One `EventRow` in the feed's event shape, so `liveAction` can read it. */
function toFeedEvent(e: EventRow): FeedEvent {
  return {
    seq: e.rowid,
    type: e.type,
    name: e.name,
    phase_id: e.phase_id,
    parent_seq: e.parent_id,
    payload: parsePayload(e.payload_json),
    tokens: e.tokens,
    started_at: e.started_at,
  };
}

/** The last `log` event with a non-blank string `payload.prompt`. */
function lastPromptLog(events: EventRow[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type !== "log") continue;
    const prompt = nonEmptyStr(parsePayload(e.payload_json)?.prompt);
    if (prompt !== null) return prompt;
  }
  return null;
}

/** The last `handoff`/`plan_provenance` event with a non-blank `payload.prompt`. */
function lastPlanProvenance(events: EventRow[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type !== "handoff" || e.name !== "plan_provenance") continue;
    const prompt = nonEmptyStr(parsePayload(e.payload_json)?.prompt);
    if (prompt !== null) return prompt;
  }
  return null;
}
