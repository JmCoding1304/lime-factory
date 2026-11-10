import type { AgentStartPayload, EventRow, ToolCallPayload } from "./types";

// ── Payload parsing ──────────────────────────────────────────────────────────

export function parsePayload(
  raw: string | null | undefined,
): Record<string, unknown> | null {
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    /* legacy or truncated payloads render raw */
  }
  return null;
}

export function parseToolCall(e: EventRow): ToolCallPayload | null {
  const payload = parsePayload(e.payload_json);
  if (!payload || typeof payload.tool !== "string") return null;
  return payload as ToolCallPayload;
}

export function parseAgentStart(e: EventRow): AgentStartPayload | null {
  return parsePayload(e.payload_json) as AgentStartPayload | null;
}

// Args keys most likely to BE the call, in priority order — a bash command, a
// file path, a search pattern. Used to build the one-line label.
const ARG_LABEL_KEYS = [
  "command",
  "cmd",
  "file_path",
  "path",
  "pattern",
  "query",
  "url",
  "prompt",
  "description",
];

const LABEL_MAX = 160;

function oneLine(value: string): string {
  const flat = value.replaceAll(/\s+/g, " ").trim();
  return flat.length > LABEL_MAX ? `${flat.slice(0, LABEL_MAX)}…` : flat;
}

/** One-line summary of a tool's args: the call itself, not a JSON dump. */
export function argsSummary(args: Record<string, unknown> | undefined): string {
  if (!args) return "";
  for (const key of ARG_LABEL_KEYS) {
    const v = args[key];
    if (typeof v === "string" && v.trim() !== "") return oneLine(v);
  }
  const parts: string[] = [];
  for (const [key, v] of Object.entries(args)) {
    if (v == null) continue;
    parts.push(`${key}=${typeof v === "string" ? v : JSON.stringify(v)}`);
  }
  return oneLine(parts.join(" "));
}
