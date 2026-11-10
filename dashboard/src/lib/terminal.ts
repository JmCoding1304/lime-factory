// The read-only terminal's lines: pure, no DOM. Mounted by 267.
import type { FeedEvent } from "./types";
import { isCashModel } from "./types";
import { argsSummary } from "./events";
import { oneLine } from "./stream";
import { modelName } from "./models";
import { fmtBytes, fmtCallMs, fmtClock, fmtCost, fmtK, fmtMs, ts } from "./format";

export type TermKind = "cmd" | "rule" | "comment" | "ok" | "err" | "warn";

export interface TermLine {
  id: number; // FeedEvent.seq of the line's own event: the :key
  ts: string; // fmtClock(started_at), "" when started_at is null
  kind: TermKind;
  tool: string | null; // non-Bash agent call: lower-case tool name, drawn after ❯
  toolCls: string; // TOOL_CLS colour class, "" when tool is null
  text: string; // the one line, without the ❯ / ── / # decoration
  ok: boolean | null; // ✓ / ✕ / no mark
  right: string; // "1.2s", "exit 0 · 4m 07s", "" when none
  detail: string[] | null; // expanded: the call (command lines or args JSON)
  out: string[] | null; // expanded: the output tail lines
  meta: string[]; // expanded: dim "# …" facts (size, log path, no result)
}

export const TOOL_CLS: Record<string, string> = {
  read: "t-read",
  grep: "t-grep",
  glob: "t-grep",
  find: "t-grep",
  ls: "t-ls",
  edit: "t-edit",
  write: "t-edit",
  multiedit: "t-edit",
  notebookedit: "t-edit",
  task: "t-ls",
  webfetch: "t-ls",
  websearch: "t-ls",
};

/** A payload that is an object (not an array, not null) becomes a record. */
function rec(v: unknown): Record<string, unknown> | null {
  if (v && typeof v === "object" && !Array.isArray(v)) {
    return v as Record<string, unknown>;
  }
  return null;
}

/** A finite number, else 0. */
function num(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

/** The skeleton every line starts from. */
function base(e: FeedEvent): TermLine {
  return {
    id: e.seq,
    ts: e.started_at == null ? "" : fmtClock(e.started_at),
    kind: "comment",
    tool: null,
    toolCls: "",
    text: "",
    ok: null,
    right: "",
    detail: null,
    out: null,
    meta: [],
  };
}

/** The quality / log command spine: a command, an exit, a took, a log path. */
function spineLine(e: FeedEvent, p: Record<string, unknown>): TermLine {
  const line = base(e);
  line.kind = "cmd";
  const command = String(p.command ?? "");
  line.text = oneLine(command, 400);

  const returncode = typeof p.returncode === "number" ? p.returncode : null;
  if (typeof p.passed === "boolean") line.ok = p.passed;
  else if (returncode !== null) line.ok = returncode === 0;

  const start = typeof p.started_at === "string" ? ts(p.started_at) : NaN;
  const end = typeof p.ended_at === "string" ? ts(p.ended_at) : NaN;
  const took = Number.isFinite(start) && Number.isFinite(end) ? fmtMs(end - start) : "";
  line.right = [returncode !== null ? `exit ${returncode}` : null, took || null]
    .filter(Boolean)
    .join(" · ");

  line.detail = command.split("\n");
  line.out = null;

  const name = e.name ?? "";
  const stripped = name.startsWith("quality:") ? name.slice("quality:".length) : name;
  line.meta = [
    stripped ? `# ${stripped}` : null,
    took ? `# took ${took}` : null,
    typeof p.output_artifact === "string" ? `# log ${p.output_artifact}` : null,
  ].filter((v): v is string => v !== null);
  return line;
}

export function terminalLines(
  events: FeedEvent[],
  phaseId: string | null = null,
): TermLine[] {
  const sorted = events.toSorted((a, b) => a.seq - b.seq);
  const filtered =
    phaseId === null ? sorted : sorted.filter((e) => e.phase_id === phaseId);

  const out: TermLine[] = [];
  const open = new Map<string, TermLine>();
  const phaseStart = new Map<string | null, string | null>();
  const model = new Map<string | null, string | null>();

  for (const e of filtered) {
    const p = rec(e.payload);
    const type = e.type;

    // ── tool_call ───────────────────────────────────────────────────────
    if (type === "tool_call") {
      if (p && typeof p.tool === "string") {
        // agent call
        const args = rec(p.args) ?? {};
        const lower = p.tool.toLowerCase();
        const line = base(e);
        line.kind = "cmd";
        if (lower === "bash") {
          line.text = oneLine(String(args.command ?? ""), 400);
          line.detail = String(args.command ?? "").split("\n");
          if (typeof args.description === "string" && args.description !== "") {
            line.meta.push(`# ${args.description}`);
          }
        } else {
          line.tool = lower;
          line.toolCls = TOOL_CLS[lower] ?? "t-dim";
          line.text = argsSummary(args);
          line.detail = JSON.stringify(args, null, 2).split("\n");
        }
        line.ok = typeof p.ok === "boolean" ? p.ok : null;
        line.right = "";
        line.out = null;
        line.meta.push("# no result recorded");
        if (typeof p.tool_call_id === "string" && p.tool_call_id !== "") {
          open.set(`${e.phase_id ?? ""}|${p.tool_call_id}`, line);
        }
        out.push(line);
        continue;
      }
      if (p && typeof p.command === "string") {
        // quality command
        out.push(spineLine(e, p));
        continue;
      }
      // neither shape
      const line = base(e);
      line.kind = "cmd";
      line.tool = e.name ?? "tool";
      line.toolCls = "t-dim";
      line.text = argsSummary(p ?? {});
      line.detail = p ? JSON.stringify(p, null, 2).split("\n") : null;
      out.push(line);
      continue;
    }

    // ── tool_result: folds into its open call, draws nothing itself ──────
    if (type === "tool_result") {
      if (!p || typeof p.tool_call_id !== "string") continue;
      const key = `${e.phase_id ?? ""}|${p.tool_call_id}`;
      const line = open.get(key);
      if (!line) continue;
      line.ok = typeof p.ok === "boolean" ? p.ok : null;
      if (typeof p.duration_ms === "number" && Number.isFinite(p.duration_ms)) {
        line.right = fmtCallMs(p.duration_ms);
      }
      const tail = typeof p.tail === "string" ? p.tail : "";
      if (tail) {
        const cut = tail.endsWith("\n") ? tail.slice(0, -1) : tail;
        line.out = cut.split("\n");
      } else {
        line.out = null;
      }
      const outBytes = typeof p.out_bytes === "number" ? p.out_bytes : 0;
      let size: string;
      if (outBytes === 0) {
        size = "# no output";
      } else {
        const tailBytes = new TextEncoder().encode(tail).length;
        size =
          tailBytes < outBytes
            ? `# output ${fmtBytes(outBytes)} · last ${fmtBytes(tailBytes)} shown`
            : `# output ${fmtBytes(outBytes)}`;
      }
      const at = line.meta.indexOf("# no result recorded");
      if (at >= 0) line.meta[at] = size;
      else line.meta.push(size);
      open.delete(key);
      continue;
    }

    // ── log ─────────────────────────────────────────────────────────────
    if (type === "log") {
      if (e.name === "rescope_recovery_decision") {
        const line = base(e);
        line.kind = "warn";
        line.text = `◆ rescope decision · ${oneLine(String(p?.decision ?? ""), 160)}`;
        out.push(line);
        continue;
      }
      if (p && typeof p.prompt === "string") {
        const line = base(e);
        line.kind = "comment";
        line.text = `prompt · ${oneLine(p.prompt, 150)}`;
        line.detail = p.prompt.split("\n");
        out.push(line);
        continue;
      }
      if (p && typeof p.command === "string") {
        out.push(spineLine(e, p));
        continue;
      }
      const msg = String(p?.message ?? "").trim();
      if (!msg) {
        if (p) {
          const rest: Record<string, unknown> = {};
          for (const [k, v] of Object.entries(p)) {
            if (k === "level" || k === "agent" || k === "message") continue;
            rest[k] = v;
          }
          if (Object.keys(rest).length > 0) {
            const line = base(e);
            line.kind = "comment";
            line.text = `${e.name ?? "log"} · ${argsSummary(rest)}`;
            line.detail = JSON.stringify(rest, null, 2).split("\n");
            out.push(line);
          }
        }
        continue;
      }
      if (/^(▶|▸|✓|✕|✗)\s/.test(msg)) continue;
      const line = base(e);
      line.kind =
        p?.level === "error" ? "err" : p?.level === "warning" ? "warn" : "comment";
      line.text = oneLine(msg, 220);
      line.detail =
        msg.length > 220 || msg.includes("\n") ? msg.split("\n") : null;
      out.push(line);
      continue;
    }

    // ── handoff ─────────────────────────────────────────────────────────
    if (type === "handoff") {
      if (e.name === "plan_provenance") {
        if (!p || typeof p.prompt !== "string") continue;
        const line = base(e);
        line.kind = "comment";
        line.text = `brief · ${oneLine(p.prompt, 150)}`;
        line.detail = p.prompt.split("\n");
        out.push(line);
        continue;
      }
      if (e.name === "needs_rescope") {
        const summary = p && typeof p.summary === "string" ? p.summary : "";
        const path = p && typeof p.path === "string" ? p.path : null;
        const line = base(e);
        line.kind = "warn";
        line.text = `◆ needs rescope · ${oneLine(summary || path || "", 160)}`;
        line.detail = [
          ...(summary ? summary.split("\n") : []),
          "",
          `handoff: ${path ?? "unavailable"}`,
        ];
        out.push(line);
        continue;
      }
      const summary = p && typeof p.summary === "string" ? p.summary : "";
      const artifacts = p && Array.isArray(p.artifacts) ? (p.artifacts as unknown[]) : null;
      const detail: string[] = [];
      if (summary) detail.push(...summary.split("\n"));
      if (artifacts && artifacts.length > 0) {
        detail.push("", "artifacts:");
        for (const a of artifacts) {
          detail.push("  " + (typeof a === "string" ? a : JSON.stringify(a)));
        }
      }
      const line = base(e);
      line.kind = "comment";
      line.text = `handoff · ${oneLine(summary, 160)}`;
      line.detail = detail.length ? detail : null;
      out.push(line);
      continue;
    }

    // ── gate_pass / gate_fail ───────────────────────────────────────────
    if (type === "gate_pass" || type === "gate_fail") {
      const violations = p && Array.isArray(p.violations) ? (p.violations as unknown[]) : [];
      const checks = p && Array.isArray(p.checks) ? (p.checks as unknown[]) : [];
      const pass = type === "gate_pass";
      const line = base(e);
      line.kind = pass ? "ok" : "err";
      line.text = `gate · ${
        violations.length
          ? violations.join("; ")
          : `${checks.length} checks ${pass ? "passed" : "failed"}`
      }`;
      const detail: string[] = [];
      for (const ck of checks) {
        const cr = rec(ck);
        if (!cr) continue;
        const note = typeof cr.note === "string" && cr.note ? `  ${cr.note}` : "";
        detail.push(`${cr.ok ? "✓" : "✕"} ${String(cr.item ?? "")}${note}`);
      }
      line.detail = detail.length ? detail : null;
      out.push(line);
      continue;
    }

    // ── error ───────────────────────────────────────────────────────────
    if (type === "error") {
      const text = String(p?.error ?? p?.reason ?? "error");
      const line = base(e);
      line.kind = "err";
      line.text = `✕ ${oneLine(text, 200)}`;
      line.detail = text.length > 200 || text.includes("\n") ? text.split("\n") : null;
      out.push(line);
      continue;
    }

    // ── crash_detected ──────────────────────────────────────────────────
    if (type === "crash_detected") {
      const line = base(e);
      line.kind = "err";
      line.text = `✕ crash · ${String(p?.signal ?? "signal")} · exit ${String(p?.exit_code)}`;
      out.push(line);
      continue;
    }

    // ── phase_start ─────────────────────────────────────────────────────
    if (type === "phase_start") {
      const line = base(e);
      line.kind = "rule";
      line.text = `▶ ${e.name ?? "phase"}`;
      phaseStart.set(e.phase_id, e.started_at);
      out.push(line);
      continue;
    }

    // ── phase_end ───────────────────────────────────────────────────────
    if (type === "phase_end") {
      const status = p?.status;
      const line = base(e);
      line.kind = "rule";
      line.text = `${e.name ?? "phase"} · ${String(status ?? "end")}`;
      line.ok = status === "success" ? true : status === "fail" ? false : null;
      const startIso = phaseStart.get(e.phase_id);
      const start = startIso ? ts(startIso) : NaN;
      const end = e.started_at ? ts(e.started_at) : NaN;
      line.right = Number.isFinite(start) && Number.isFinite(end) ? fmtMs(end - start) : "";
      out.push(line);
      continue;
    }

    // ── agent_start ─────────────────────────────────────────────────────
    if (type === "agent_start") {
      const m = p && typeof p.model === "string" ? p.model : null;
      model.set(e.phase_id, m);
      const thinking = p && typeof p.thinking === "string" ? p.thinking : "";
      const runtime = p && typeof p.runtime === "string" ? p.runtime : "";
      const line = base(e);
      line.kind = "rule";
      line.text = [e.name, m ? modelName(m) : "", thinking, runtime]
        .filter(Boolean)
        .join(" · ");
      out.push(line);
      continue;
    }

    // ── agent_end ───────────────────────────────────────────────────────
    if (type === "agent_end") {
      const status = p && typeof p.status === "string" ? p.status : null;
      const cost = p && typeof p.cost === "number" ? p.cost : null;
      const ctx = num(p?.context_tokens);
      const m = model.get(e.phase_id) ?? null;
      const line = base(e);
      line.kind = "rule";
      line.text = [
        "end",
        status,
        cost !== null ? (isCashModel(m) ? fmtCost(cost) : "subscription") : null,
        ctx > 0 ? `${fmtK(ctx)} context` : null,
      ]
        .filter(Boolean)
        .join(" · ");
      out.push(line);
      continue;
    }

    // ── budget_warn ─────────────────────────────────────────────────────
    if (type === "budget_warn") {
      const line = base(e);
      line.kind = "warn";
      line.text = `▲ context ${fmtK(num(p?.context_tokens))} passed the warn line (${fmtK(
        num(p?.warn),
      )})`;
      out.push(line);
      continue;
    }

    // ── budget_stop ─────────────────────────────────────────────────────
    if (type === "budget_stop") {
      const line = base(e);
      line.kind = "err";
      line.text = `■ budget stop · context ${fmtK(num(p?.context_tokens))} passed the stop line (${fmtK(
        num(p?.stop),
      )})`;
      out.push(line);
      continue;
    }

    // ── budget_kill ─────────────────────────────────────────────────────
    if (type === "budget_kill") {
      const line = base(e);
      line.kind = "err";
      line.text = `■ budget kill · context ${fmtK(num(p?.context_tokens))}`;
      out.push(line);
      continue;
    }

    // ── timeout_nudge ───────────────────────────────────────────────────
    if (type === "timeout_nudge") {
      const line = base(e);
      line.kind = "warn";
      line.text =
        `⏱ time ceiling · nudged after ${fmtMs(num(p?.elapsed_s) * 1000)}` +
        (p?.reason === "repeat" ? " · repeat" : "");
      out.push(line);
      continue;
    }

    // ── timeout_kill ────────────────────────────────────────────────────
    if (type === "timeout_kill") {
      const line = base(e);
      line.kind = "err";
      let text = `⏱ timeout kill · ${fmtMs(num(p?.elapsed_s) * 1000)}`;
      if (p?.reason === "repeat") text += " · repeat";
      else if (num(p?.timeout_s) > 0) text += ` · ceiling ${fmtMs(num(p?.timeout_s) * 1000)}`;
      line.text = text;
      out.push(line);
      continue;
    }

    // ── repeat_stop ─────────────────────────────────────────────────────
    if (type === "repeat_stop") {
      const line = base(e);
      line.kind = "err";
      line.text = `■ repeat stop · ${String(p?.tool ?? "tool")} × ${String(p?.count ?? 3)}`;
      out.push(line);
      continue;
    }

    // ── continuation ────────────────────────────────────────────────────
    if (type === "continuation") {
      const line = base(e);
      line.kind = "warn";
      let text = `↻ fresh session ${String(p?.continuation ?? "")}`;
      if (p?.reason) text += ` · ${String(p.reason)}`;
      if (p?.error) text += ` · ${oneLine(String(p.error), 100)}`;
      line.text = text;
      out.push(line);
      continue;
    }

    // ── turn: no line (PLACEMENT #49) ───────────────────────────────────
    if (type === "turn") continue;

    // ── any other or unknown type ───────────────────────────────────────
    const line = base(e);
    line.kind = "comment";
    line.text = `${e.type ?? "event"} · ${argsSummary(p ?? {})}`;
    out.push(line);
  }

  return out;
}

// ── the window, the tail and the size ─────────────────────────────────────

export const PAGE = 500;
export const TERM_H_KEY = "lf-term-h";
export const TERM_COLLAPSED_KEY = "lf-term-collapsed";
export const TERM_MIN = 96; // px, smallest open height
export const TERM_HEAD = 34; // px, the bar = the collapsed height
export const TERM_SHARE = 0.4; // default: 40% of the parent (NOTES §12)
export const CANVAS_KEEP = 150; // px the canvas always keeps above
export const TERM_FALLBACK_H = 280; // parent not measured yet

export interface TermWindow {
  shown: TermLine[];
  hidden: number;
}

/** The newest `limit` lines; the rest are hidden behind "Show earlier". */
export function windowLines(lines: TermLine[], limit: number): TermWindow {
  const hidden = Math.max(0, lines.length - limit);
  return { shown: lines.slice(hidden), hidden };
}

/** "Show earlier" grows the window by one page. */
export function showEarlier(limit: number): number {
  return limit + PAGE;
}

export interface TailState {
  limit: number;
  unseen: number;
}

/** The window slides while the reader follows; otherwise the top stays put. */
export function onNewLines(s: TailState, added: number, following: boolean): TailState {
  if (added <= 0) return s;
  if (following) return { limit: s.limit, unseen: 0 };
  return { limit: s.limit + added, unseen: s.unseen + added };
}

export function jumpLabel(unseen: number): string {
  return unseen === 0 ? "Jump to latest" : `Jump to latest · ${unseen} new`;
}

export function emptyText(phaseId: string | null, queued: boolean): string {
  if (queued) return "not started yet";
  if (phaseId !== null) return "no events for this phase";
  return "no events yet";
}

export interface TermPrefs {
  height: number | null;
  collapsed: boolean;
}

export function loadTermPrefs(storage: Pick<Storage, "getItem"> | null): TermPrefs {
  let height: number | null = null;
  let collapsed = false;
  try {
    const rawH = storage?.getItem(TERM_H_KEY) ?? null;
    if (rawH !== null) {
      const n = Number(rawH);
      if (Number.isFinite(n) && n > 0) height = n;
    }
    collapsed = storage?.getItem(TERM_COLLAPSED_KEY) === "1";
  } catch {
    /* a malformed or unreadable pref opens the default (40%, expanded) */
  }
  return { height, collapsed };
}

export function saveTermHeight(storage: Pick<Storage, "setItem"> | null, h: number): void {
  try {
    storage?.setItem(TERM_H_KEY, String(Math.round(h)));
  } catch {
    /* a failed write keeps the in-memory height */
  }
}

export function saveTermCollapsed(storage: Pick<Storage, "setItem"> | null, c: boolean): void {
  try {
    storage?.setItem(TERM_COLLAPSED_KEY, c ? "1" : "0");
  } catch {
    /* a failed write keeps the in-memory collapsed state */
  }
}

export function clampTermHeight(h: number | null, parentH: number): number {
  if (parentH <= 0) return Math.max(TERM_MIN, h ?? TERM_FALLBACK_H);
  const want = h ?? Math.round(parentH * TERM_SHARE);
  const lo = TERM_MIN;
  const hi = Math.max(TERM_MIN, parentH - CANVAS_KEEP);
  return Math.round(Math.min(hi, Math.max(lo, want)));
}
