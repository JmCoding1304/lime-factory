import type { EventRow } from "./types";
import { parsePayload, parseToolCall, argsSummary } from "./events";
import { fmtCost, fmtK } from "./format";
import { modelName } from "./models";

export type StreamKind = "start" | "turn" | "tool" | "text" | "gate" | "error" | "end" | "note";

export interface StreamRow {
  /** event_id — the :key and the tail marker. */
  id: number;
  kind: StreamKind;
  /** Left column: the actor or the tool, e.g. "bash", "turn 3", "quality:test". */
  label: string;
  /** One line, already flattened — the call or the sentence itself. */
  summary: string;
  /** Expandable block; null when the row has nothing more to show. */
  detail: string | null;
  /** false → paint the label with --bad, true → --ok, null → neutral. */
  ok: boolean | null;
}

const ONE_LINE_MAX = 160;

/** Flatten multi-line text: collapse whitespace, cap at `max` chars with "…". */
export function oneLine(value: string, max = ONE_LINE_MAX): string {
  const flat = value.replaceAll(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}

function prettyPayload(payload: Record<string, unknown> | null): string | null {
  if (!payload) return null;
  return JSON.stringify(payload, null, 2);
}

export function streamRows(events: EventRow[]): StreamRow[] {
  const sorted = events.toSorted((a, b) => a.event_id - b.event_id);
  const out: StreamRow[] = [];

  for (const e of sorted) {
    const payload = parsePayload(e.payload_json);

    // ── agent_start ─────────────────────────────────────────────────────
    if (e.type === "agent_start") {
      const model = payload ? modelName(payload.model as string | undefined) : "";
      const purpose = payload ? (payload.purpose as string | undefined) : "";
      const runtime = payload ? (payload.runtime as string | undefined) : "";
      const parts: string[] = [];
      if (model) parts.push(model);
      if (purpose || runtime) {
        const m = [purpose, runtime].filter(Boolean).join(" · ");
        if (m) parts.push(m);
      }
      out.push({
        id: e.event_id,
        kind: "start",
        label: e.name ?? "agent",
        summary: parts.join(" · ") || "",
        detail: prettyPayload(payload),
        ok: null,
      });
      continue;
    }

    // ── turn ────────────────────────────────────────────────────────────
    if (e.type === "turn") {
      const turnNo = payload ? (payload.turn_no as number | undefined) : undefined;
      const input = payload ? (payload.input as number | undefined) : undefined;
      const output = payload ? (payload.output as number | undefined) : undefined;
      const cost = payload ? (payload.cost as number | undefined) : undefined;
      const parts: string[] = [];
      if (input != null) parts.push(`in ${fmtK(input)}`);
      if (output != null) parts.push(`out ${fmtK(output)}`);
      if (cost != null) parts.push(fmtCost(cost));
      out.push({
        id: e.event_id,
        kind: "turn",
        label: turnNo != null ? `turn ${turnNo}` : "turn",
        summary: parts.join(" · ") || "",
        detail: prettyPayload(payload),
        ok: null,
      });
      continue;
    }

    // ── tool_call ───────────────────────────────────────────────────────
    if (e.type === "tool_call") {
      if (payload && typeof payload.tool === "string") {
        // Agent tool call
        const call = parseToolCall(e);
        const args = call?.args ?? {};
        const resultSnippet = call?.result_snippet;
        let detail = JSON.stringify(args, null, 2);
        if (resultSnippet) detail += `\n\n→ ${resultSnippet}`;
        out.push({
          id: e.event_id,
          kind: "tool",
          label: payload.tool as string,
          summary: argsSummary(args),
          detail,
          ok: payload.ok === false ? false : payload.ok === true ? true : null,
        });
      } else if (payload && typeof payload.command === "string") {
        // Quality tool call
        const returncode = payload.returncode as number | undefined;
        const outputArtifact = payload.output_artifact as string | undefined;
        const detailParts: string[] = [];
        if (returncode != null) detailParts.push(`exit ${returncode}`);
        if (outputArtifact) detailParts.push(outputArtifact);
        out.push({
          id: e.event_id,
          kind: "tool",
          label: e.name ?? "command",
          summary: payload.command as string,
          detail: detailParts.length ? detailParts.join("\n") : null,
          ok: payload.passed === false ? false : payload.passed === true ? true : null,
        });
      } else {
        // Neither shape
        out.push({
          id: e.event_id,
          kind: "tool",
          label: e.name ?? "tool",
          summary: argsSummary(payload ?? {}),
          detail: prettyPayload(payload),
          ok: null,
        });
      }
      continue;
    }

    // ── handoff ─────────────────────────────────────────────────────────
    if (e.type === "handoff") {
      const summary = payload ? (payload.summary as string | undefined) ?? "" : "";
      const artifacts = payload ? (payload.artifacts as string[] | undefined) : undefined;
      const detailParts: string[] = [];
      if (summary) detailParts.push(summary);
      if (artifacts && artifacts.length > 0) {
        if (detailParts.length) detailParts.push("");
        detailParts.push(...artifacts);
      }
      out.push({
        id: e.event_id,
        kind: "text",
        label: e.name ?? "handoff",
        summary: oneLine(summary),
        detail: detailParts.length ? detailParts.join("\n") : null,
        ok: null,
      });
      continue;
    }

    // ── log ─────────────────────────────────────────────────────────────
    if (e.type === "log") {
      const prompt = payload ? (payload.prompt as string | undefined) : undefined;
      const message = payload ? (payload.message as string | undefined) : undefined;
      if (typeof prompt === "string") {
        out.push({
          id: e.event_id,
          kind: "text",
          label: e.name ?? "prompt",
          summary: oneLine(prompt),
          detail: prompt,
          ok: null,
        });
      } else if (typeof message === "string") {
        out.push({
          id: e.event_id,
          kind: "note",
          label: e.name ?? "log",
          summary: oneLine(message),
          detail: null,
          ok: (payload?.level as string | undefined) === "error" ? false : null,
        });
      } else {
        out.push({
          id: e.event_id,
          kind: "note",
          label: e.name ?? "log",
          summary: argsSummary(payload ?? {}),
          detail: prettyPayload(payload),
          ok: null,
        });
      }
      continue;
    }

    // ── gate_pass / gate_fail ───────────────────────────────────────────
    if (e.type === "gate_pass" || e.type === "gate_fail") {
      const violations = payload
        ? (payload.violations as string[] | undefined)
        : undefined;
      const checks = payload ? (payload.checks as unknown[] | undefined) : undefined;
      let summary: string;
      if (violations && violations.length > 0) {
        summary = violations.join("; ");
      } else if (checks && checks.length > 0) {
        summary = `${checks.length} checks`;
      } else {
        summary = e.type === "gate_pass" ? "passed" : "failed";
      }
      const hasDetail =
        (violations && violations.length > 0) || (checks && checks.length > 0);
      out.push({
        id: e.event_id,
        kind: "gate",
        label: e.name ?? "gate",
        summary,
        detail: hasDetail ? prettyPayload(payload) : null,
        ok: e.type === "gate_pass",
      });
      continue;
    }

    // ── error ───────────────────────────────────────────────────────────
    if (e.type === "error") {
      const errText = payload
        ? ((payload.error as string | undefined) ??
            (payload.reason as string | undefined) ??
            "")
        : "";
      const fullText = errText;
      const summary = oneLine(errText);
      out.push({
        id: e.event_id,
        kind: "error",
        label: e.name ?? "error",
        summary,
        detail: fullText.length > summary.length ? fullText : null,
        ok: false,
      });
      continue;
    }

    // ── agent_end ───────────────────────────────────────────────────────
    if (e.type === "agent_end") {
      const cost = payload ? (payload.cost as number | undefined) : undefined;
      const contextTokens = payload
        ? (payload.context_tokens as number | undefined)
        : undefined;
      const parts: string[] = [];
      if (cost != null) parts.push(fmtCost(cost));
      if (contextTokens != null) parts.push(`${fmtK(contextTokens)} ctx`);
      out.push({
        id: e.event_id,
        kind: "end",
        label: e.name ?? "agent_end",
        summary: parts.join(" · ") || "",
        detail: prettyPayload(payload),
        ok: null,
      });
      continue;
    }

    // ── anything else ───────────────────────────────────────────────────
    out.push({
      id: e.event_id,
      kind: "note",
      label: e.type ?? e.name ?? "event",
      summary: argsSummary(payload ?? {}),
      detail: null,
      ok: null,
    });
  }

  return out;
}