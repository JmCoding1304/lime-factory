import type { EventRow } from "./types";
import { parsePayload } from "./events";
import { fmtK, fmtMs } from "./format";

/**
 * The Run header's stop/kill/crash signals, beside the phase-node `warn` tick.
 * One chip per type present, always in a fixed order, so the header never
 * reflows as events arrive. `budget_warn` is the only warning; a budget stop, a
 * timeout kill and a crash are all bad.
 */
export interface RunSignal {
  type: "budget_warn" | "budget_stop" | "timeout_kill" | "crash_detected";
  label: string;
  count: number;
  tone: "warn" | "bad";
  title: string;
}

/** The one order the header draws them in. */
const ORDER = [
  "budget_warn",
  "budget_stop",
  "timeout_kill",
  "crash_detected",
] as const;

type SignalType = (typeof ORDER)[number];

const LABELS: Record<SignalType, string> = {
  budget_warn: "▲ warn",
  budget_stop: "■ budget stop",
  timeout_kill: "⏱ timeout kill",
  crash_detected: "✕ crash",
};

/** One line per event: its `name`, plus the distinguishing payload fields. */
function titleFor(type: SignalType, events: EventRow[]): string {
  return events
    .map((e) => {
      const name = e.name ?? "";
      const p = parsePayload(e.payload_json);
      if (type === "timeout_kill") {
        let line = `${name} ${p?.elapsed_s}s`;
        if (p?.reason === "repeat") line += " (repeat)";
        return line;
      }
      if (type === "crash_detected") {
        return `${name} signal ${p?.signal} · exit ${p?.exit_code}`;
      }
      return name;
    })
    .join("\n");
}

/** A number only when the payload field really is a finite number. */
function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** Non-blank string after trimming, else null. */
function str(v: unknown): string | null {
  if (typeof v !== "string") return null;
  const t = v.trim();
  return t === "" ? null : v;
}

/**
 * One phase's stop/warn signals, in the order its events arrived. Each entry
 * is derived from one event; a missing or non-number field drops the number
 * and falls back to the bare label, never throwing.
 */
export interface PhaseSignal {
  type:
    | "budget_warn"
    | "budget_stop"
    | "timeout_kill"
    | "crash_detected"
    | "continuation"
    | "needs_rescope";
  glyph: string;
  text: string;
  tone: "warn" | "bad";
}

/** Collapse whitespace and clamp to 90 chars with a trailing ellipsis. */
function clamp(text: string): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > 90 ? `${flat.slice(0, 90)}…` : flat;
}

export function phaseSignals(events: EventRow[]): PhaseSignal[] {
  const out: PhaseSignal[] = [];
  for (const e of events) {
    const p = parsePayload(e.payload_json);
    if (e.type === "budget_warn") {
      const tokens = num(p?.context_tokens);
      const warn = num(p?.warn);
      out.push({
        type: "budget_warn",
        glyph: "▲",
        tone: "warn",
        text:
          tokens !== null && warn !== null
            ? `context ${fmtK(tokens)} passed the ${fmtK(warn)} warn line`
            : "budget warning",
      });
    } else if (e.type === "budget_stop") {
      const tokens = num(p?.context_tokens);
      const stop = num(p?.stop);
      out.push({
        type: "budget_stop",
        glyph: "■",
        tone: "bad",
        text:
          tokens !== null && stop !== null
            ? `budget stop at ${fmtK(tokens)} (stop ${fmtK(stop)})`
            : "budget stop",
      });
    } else if (e.type === "timeout_kill") {
      const elapsed = num(p?.elapsed_s);
      let text =
        elapsed !== null ? `timeout kill after ${fmtMs(elapsed * 1000)}` : "timeout kill";
      if (elapsed !== null && p?.reason === "repeat") text += " (repeat)";
      out.push({ type: "timeout_kill", glyph: "⏱", tone: "bad", text });
    } else if (e.type === "crash_detected") {
      const signal = num(p?.signal);
      const exit = num(p?.exit_code);
      out.push({
        type: "crash_detected",
        glyph: "✕",
        tone: "bad",
        text:
          signal !== null && exit !== null
            ? `crash · signal ${signal} · exit ${exit}`
            : "crash",
      });
    } else if (e.type === "continuation") {
      const continuation = num(p?.continuation);
      let text = "fresh session";
      if (continuation !== null) text += ` ${continuation}`;
      const reason = str(p?.reason);
      if (reason !== null) text += ` · ${reason}`;
      const error = str(p?.error);
      if (error !== null) text += ` · ${error}`;
      out.push({ type: "continuation", glyph: "↻", tone: "warn", text: clamp(text) });
    } else if (e.type === "handoff" && e.name === "needs_rescope") {
      let text = "needs rescope";
      if (typeof p?.path === "string") text += ` · ${p.path}`;
      out.push({ type: "needs_rescope", glyph: "◆", tone: "warn", text });
    }
  }
  return out;
}

export function runSignals(events: EventRow[]): RunSignal[] {
  const out: RunSignal[] = [];
  for (const type of ORDER) {
    const matching = events.filter((e) => e.type === type);
    if (!matching.length) continue;
    out.push({
      type,
      label:
        matching.length > 1
          ? `${LABELS[type]} ×${matching.length}`
          : LABELS[type],
      count: matching.length,
      tone: type === "budget_warn" ? "warn" : "bad",
      title: titleFor(type, matching),
    });
  }
  return out;
}
