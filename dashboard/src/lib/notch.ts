import type { LiveRunning, RunningRun } from "./types";
import { runName } from "./runsSheet";
import { modelName } from "./models";

/** Fill at or above this percent is drawn hot. */
export const HOT_PCT = 80;

/** A `batch_children` queued row, derived from the wire type. */
export type QueuedChild = NonNullable<LiveRunning["queued"]>[number];
/** A live batch row, derived from the wire type. */
export type LiveBatch = NonNullable<LiveRunning["batches"]>[number];

/** One expanded-notch row: a running Run, or a dimmed queued child. */
export type NotchItem =
  | { type: "run"; key: string; run: RunningRun }
  | { type: "queued"; key: string; child: QueuedChild; word: string | null };

/** One expanded-notch group: optional batch header plus its ordered rows. */
export interface NotchGroup {
  key: string;
  header: string | null;
  items: NotchItem[];
}

/** A Run's compact/expanded notch view: feed order preserved. */
export interface NotchView {
  rows: RunningRun[];
  first: RunningRun | null;
  more: number;
  groups: NotchGroup[];
}

/**
 * The notch's rows: the whole `running` frame in feed order. A null frame or an
 * empty list both yield the tiny pill (`first === null`, `more === 0`). `rows`,
 * `first` and `more` count running Runs only; `groups` add the batched view.
 */
export function notchView(frame: LiveRunning | null): NotchView {
  const runs = frame?.runs ?? [];
  const first = runs[0] ?? null;
  return {
    rows: runs,
    first,
    more: Math.max(0, runs.length - 1),
    groups: notchGroups(frame),
  };
}

/**
 * The one wait word for a queued child's reason, or null when there is none.
 * `slot` is next in line; `blocked X` waits after X; any other `overlap …` is
 * held — except the dropped reason `batch.OVERLAP_CHECK_FAILED`.
 */
export function waitWord(reason: string | null): string | null {
  if (reason === "slot") return "next";
  const blocked = /^blocked (.+)$/.exec(reason ?? "");
  if (blocked) return `after ${blocked[1]}`;
  if (
    reason !== null &&
    reason.startsWith("overlap ") &&
    reason !== "overlap check failed"
  ) {
    return "held";
  }
  return null;
}

/** A local `HH:MM`, zero-padded 24h; null on null or unparseable input. */
export function clockHHMM(iso: string | null): string | null {
  if (iso === null) return null;
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return null;
  const d = new Date(t);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** A batch's header: `batch`, or a retry with the dead batch's last beat. */
export function batchHeader(b: LiveBatch): string {
  if (b.retry_of === null) return "batch";
  const hhmm = clockHHMM(b.died_at);
  return hhmm === null
    ? "batch · retried"
    : `batch · retried · first try died ${hhmm}`;
}

/** A queued child's row label, from its ticket stem. */
export function childTitle(c: QueuedChild): { num: string | null; text: string } {
  return runName({ ticket: c.ticket, request: null, run_id: c.ticket });
}

/**
 * The expanded notch's groups, in draw order: the loose Runs first (no header),
 * then one group per live batch in frame order. A live retry also owns its dead
 * batch's id, so orphaned Runs adopted by the retry group under its header. A
 * batch with no rows draws no group; a null frame is no groups.
 */
export function notchGroups(frame: LiveRunning | null): NotchGroup[] {
  if (frame === null) return [];
  const batches = frame.batches ?? [];

  // Owner map: every live batch owns its own id; a live retry also owns the
  // dead batch's id, unless a live batch already claims that key.
  const owner = new Map<string, LiveBatch>();
  for (const b of batches) owner.set(`${b.project}\n${b.batch_id}`, b);
  for (const b of batches) {
    if (b.retry_of === null) continue;
    const key = `${b.project}\n${b.retry_of}`;
    if (!owner.has(key)) owner.set(key, b);
  }

  const loose: NotchItem[] = [];
  const slots = batches.map((b) => ({
    batch: b,
    items: [] as { sort: [number, number, number]; item: NotchItem }[],
  }));
  const groupOf = new Map<LiveBatch, (typeof slots)[number]>();
  for (const g of slots) groupOf.set(g.batch, g);

  frame.runs.forEach((run, feedIndex) => {
    const item: NotchItem = {
      type: "run",
      key: `run:${run.project}:${run.run_id}`,
      run,
    };
    const own = owner.get(`${run.project}\n${run.batch_id ?? ""}`);
    if (run.batch_id === null || own === undefined) {
      loose.push(item);
      return;
    }
    const target = groupOf.get(own);
    if (!target) return;
    target.items.push({
      sort: [
        run.batch_id === own.batch_id ? 1 : 0,
        run.batch_position ?? Number.MAX_SAFE_INTEGER,
        feedIndex,
      ],
      item,
    });
  });

  (frame.queued ?? []).forEach((child, feedIndex) => {
    const own = owner.get(`${child.project}\n${child.batch_id}`);
    if (own === undefined || own.batch_id !== child.batch_id) return;
    const target = groupOf.get(own);
    if (!target) return;
    target.items.push({
      sort: [1, child.position, feedIndex],
      item: {
        type: "queued",
        key: `q:${child.project}:${child.batch_id}:${child.position}`,
        child,
        word: waitWord(child.reason),
      },
    });
  });

  const out: NotchGroup[] = [];
  if (loose.length) out.push({ key: "loose", header: null, items: loose });
  for (const g of slots) {
    if (!g.items.length) continue;
    g.items.sort(
      (a, b) =>
        a.sort[0] - b.sort[0] ||
        a.sort[1] - b.sort[1] ||
        a.sort[2] - b.sort[2],
    );
    out.push({
      key: `b:${g.batch.project}:${g.batch.batch_id}`,
      header: batchHeader(g.batch),
      items: g.items.map((e) => e.item),
    });
  }
  return out;
}

/** Context fill percent (clamped to 0..100) and whether it is hot. */
export function ctxFill(
  ctx: RunningRun["context"],
): { pct: number; hot: boolean } {
  const tokens = ctx?.tokens ?? null;
  const warn = ctx?.warn ?? null;
  if (tokens === null || warn === null || warn <= 0) {
    return { pct: 0, hot: false };
  }
  const pct = Math.min(100, Math.max(0, (tokens / warn) * 100));
  return { pct, hot: pct >= HOT_PCT };
}

/** Elapsed `m:ss`, or `h:mm:ss` from one hour up; `—` on bad/null input. */
export function fmtElapsed(startIso: string | null, nowMs: number): string {
  const start = startIso === null ? NaN : Date.parse(startIso);
  if (!Number.isFinite(start)) return "—";
  const delta = Math.max(0, Math.floor((nowMs - start) / 1000));
  const h = Math.floor(delta / 3600);
  const m = Math.floor((delta % 3600) / 60);
  const s = delta % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/** The phase · role · model line, or a placeholder when empty. */
export function phaseLine(run: RunningRun, withModel = true): string {
  const parts = [
    run.phase?.name,
    run.session?.role,
    withModel ? modelName(run.session?.model) : "",
  ].filter((p): p is string => !!p);
  if (parts.length) return parts.join(" · ");
  return run.kind === "deploy" ? "deploy" : "starting";
}

/** The phase's stage, defaulting to `Ship` on a deploy row. */
export function stageOf(run: RunningRun): string | null {
  const stage = run.phase?.stage ?? null;
  if (stage !== null) return stage;
  return run.kind === "deploy" ? "Ship" : null;
}

/** The notch row's title: ticket number + humanized stem, or the run_id. */
export function rowTitle(run: RunningRun): { num: string | null; text: string } {
  return runName({ ticket: run.ticket, request: null, run_id: run.run_id });
}

/** The prototype's room for the centred pill, floored at the tiny width. */
export function notchRoom(layoutPx: number, sidePx: number): number {
  return Math.max(150, layoutPx + 16 - 2 * sidePx - 28);
}

/** Which compact parts fit in the given room. */
export function compactParts(room: number): { project: boolean; model: boolean } {
  return { project: room > 470, model: room >= 560 };
}
