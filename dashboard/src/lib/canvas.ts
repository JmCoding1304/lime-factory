// Pure geometry and card data for the Run canvas. No state, no DOM.
//
// Rows come from `phases.stage` (ticket 251). They are never mapped from
// phase names — the prototype's `stageOf(name)` regex is deliberately not
// copied, because a name is not a stage.

import type { Envelope, FeedEvent, Phase, Run, Session } from "./types";
import { type Rect, type View } from "./bench";
import { LIME_ICON, commandIcon, modelLogo, modelName } from "./models";
import { fmtClock, fmtDuration, ts } from "./format";
import { parsePayload } from "./events";
import { stepName } from "./failure";

export const CARD_W = 222;
export const CARD_H = 86;
export const GAP_X = 56;
export const GAP_Y = 104;
export const PAD_X = 72;
export const PAD_T = 58;
export const PAD_B = 64;
export const MAX_COLS = 6;

/** A branch run's card, and its column's geometry. */
export const MINI_W = 168;
export const MINI_H = 58;
export const TRY_PITCH = 420;
export const MINI_GAP = 22;
export const PILL_H = 26;

/** How much of the viewport a revealed card keeps clear of its edges. */
export const REVEAL_MARGIN = 24;

/**
 * The view that shows the card at `pos` inside a `w`×`h` viewport. A card
 * already inside the margins is left alone unless `centre` is set; otherwise
 * the view is centred on the card at the same zoom. A zero-size viewport (the
 * canvas not laid out yet) returns the view unchanged.
 */
export function revealView(
  v: View,
  pos: { x: number; y: number },
  w: number,
  h: number,
  centre: boolean,
  margin = REVEAL_MARGIN,
): View {
  if (w <= 0 || h <= 0) return { ...v };
  const left = v.x + pos.x * v.k;
  const top = v.y + pos.y * v.k;
  const right = left + CARD_W * v.k;
  const bottom = top + CARD_H * v.k;
  if (
    !centre &&
    left >= margin &&
    top >= margin &&
    right <= w - margin &&
    bottom <= h - margin
  ) {
    return { ...v };
  }
  return {
    k: v.k,
    x: w / 2 - (pos.x + CARD_W / 2) * v.k,
    y: h / 2 - (pos.y + CARD_H / 2) * v.k,
  };
}

export const STAGE_ROWS = ["Prepare", "Build", "Review", "Ship"] as const;
export const OTHER_ROW = "Other";
export type CanvasStage = (typeof STAGE_ROWS)[number] | typeof OTHER_ROW;

/** The row rule: case-sensitive exact match on the four stage names, and
 *  everything else — `Other`, null, undefined, or an unknown string — lands in
 *  the `Other` row. Names are never read. */
export function stageRow(stage: string | null | undefined): CanvasStage {
  for (const row of STAGE_ROWS) {
    if (stage === row) return row;
  }
  return OTHER_ROW;
}

export interface CardPos {
  x: number;
  y: number;
  row: number;
  w: number;
  h: number;
  mini: boolean;
}

export interface StageLabel {
  text: CanvasStage;
  x: number;
  y: number;
}

export interface CanvasEdge {
  id: string;
  d: string;
  head: string;
  done: boolean;
  flowing: boolean;
  broken: boolean;
  trunk: boolean;
}

export interface CanvasPill {
  key: string;
  try: number;
  x: number;
  /** The pill's centre. */
  y: number;
  open: boolean;
}

export interface CanvasLayout {
  pos: Record<string, CardPos>;
  labels: StageLabel[];
  edges: CanvasEdge[];
  pills: CanvasPill[];
  width: number;
  height: number;
  bounds: Rect;
}

/** Stable sort by `seq`, null last; equal seqs keep their input order. */
function bySeq(phases: Phase[]): Phase[] {
  return [...phases].sort((a, b) => (a.seq ?? Infinity) - (b.seq ?? Infinity));
}

// ── tries: the Run's attempts, read from the trace ───────────────────────────
//
// A resume writes one `log {replayed: true}` per reused phase within a few
// milliseconds; that cluster opens a new try. A try's own phases started after
// its start; its reused phases were replayed at its start; the phases it threw
// away carry `phase_invalidated`; a `*_decision` log names how it began.
// Only the final `_retryN` is stripped from a name when comparing steps.

/** A phase is a revise loop turn, never a planned step. */
const REVISE = /^revise_/;
/** A build step's step identity (`build_3_retry4` → `build_3`). */
const BUILD_STEP = /^build_\d+$/;
/** Replay logs further apart than this belong to different resumes. */
const RESUME_GAP_MS = 10_000;
/** A resume's `*_decision` log lands within this of the resume's first replay. */
const DECISION_WINDOW_MS = 5_000;

export interface RunTry {
  n: number;
  start: string | null;
  how: string;
  who: string | null;
  own: string[];
  reused: string[];
  endedBy: string | null;
  reason: string | null;
  failed: boolean;
}

interface Replay {
  t: number;
  iso: string | null;
  phase_id: string | null;
}

interface Resume {
  first: number;
  firstIso: string | null;
  last: number;
  members: Replay[];
}

function emptyTry(n: number, start: string | null, who: string | null): RunTry {
  return {
    n,
    start,
    how: "lime run",
    who,
    own: [],
    reused: [],
    endedBy: null,
    reason: null,
    failed: false,
  };
}

/**
 * The Run's tries, in order. Try 1 begins at −∞ for membership (every phase
 * started before the first resume is its own); each further try begins at the
 * first replay of the resume that opened it. Never throws: malformed payloads
 * and invalid timestamps are treated as absent.
 */
export function runTries(
  phases: Phase[],
  events: FeedEvent[],
  run: Pick<Run, "started_at" | "engineer"> | null = null,
): RunTry[] {
  const sorted = bySeq(phases);
  const engineer = run?.engineer ?? null;

  if (sorted.length === 0) {
    return [emptyTry(1, run?.started_at ?? null, engineer)];
  }

  // 1. the replay logs, clustered into resumes
  const replays: Replay[] = [];
  for (const e of events) {
    if (e.type !== "log") continue;
    const payload = rec(e.payload);
    if (!payload || payload.replayed !== true) continue;
    const t = ts(e.started_at);
    if (!Number.isFinite(t)) continue;
    replays.push({ t, iso: e.started_at, phase_id: e.phase_id });
  }
  replays.sort((a, b) => a.t - b.t);

  const resumes: Resume[] = [];
  for (const r of replays) {
    const cur = resumes[resumes.length - 1];
    if (!cur || r.t - cur.last > RESUME_GAP_MS) {
      resumes.push({ first: r.t, firstIso: r.iso, last: r.t, members: [r] });
    } else {
      cur.last = r.t;
      cur.members.push(r);
    }
  }

  let earliest: number = Infinity;
  let earliestIso: string | null = null;
  for (const p of sorted) {
    const t = ts(p.started_at);
    if (Number.isFinite(t) && t < earliest) {
      earliest = t;
      earliestIso = p.started_at;
    }
  }

  const starts = [-Infinity, ...resumes.map((r) => r.first)];
  const tries = starts.map((_, i) =>
    emptyTry(
      i + 1,
      i === 0 ? (run?.started_at ?? earliestIso) : resumes[i - 1].firstIso,
      engineer,
    ),
  );

  // 2. own: the last try whose start is not after the phase's start
  for (const p of sorted) {
    const t = ts(p.started_at);
    let idx = tries.length - 1;
    if (Number.isFinite(t)) {
      idx = 0;
      for (let i = 1; i < starts.length; i++) if (starts[i] <= t) idx = i;
    }
    tries[idx].own.push(p.phase_id);
  }

  // 3. reused: the previous resume's replayed phases, in phase seq order
  const rank = new Map<string, number>();
  sorted.forEach((p, i) => rank.set(p.phase_id, i));
  const beyond = Number.MAX_SAFE_INTEGER;
  for (let k = 2; k <= tries.length; k++) {
    const ids = new Set<string>();
    for (const r of resumes[k - 2].members) {
      if (r.phase_id) ids.add(r.phase_id);
    }
    tries[k - 1].reused = [...ids].sort(
      (a, b) => (rank.get(a) ?? beyond) - (rank.get(b) ?? beyond),
    );
  }

  // 4. how/who: the resume's decision log, when there is one
  for (let k = 2; k <= tries.length; k++) {
    const start = resumes[k - 2].first;
    const t = tries[k - 1];
    let how: string | null = null;
    for (const e of events) {
      if (e.type !== "log" || !e.name || !/_decision$/.test(e.name)) continue;
      const at = ts(e.started_at);
      if (!Number.isFinite(at) || Math.abs(at - start) >= DECISION_WINDOW_MS) {
        continue;
      }
      how = e.name;
      const who = rec(e.payload)?.engineer;
      if (typeof who === "string") t.who = who;
      break;
    }
    t.how = how ? how.replace(/_decision$/, "") : "resume";
  }

  // 5. endedBy/reason: the try's last started phase that failed or errored
  const errorPhases = new Set<string>();
  const errorPayload = new Map<string, Record<string, unknown> | null>();
  for (const e of events) {
    if (e.type !== "error" || !e.phase_id) continue;
    errorPhases.add(e.phase_id);
    errorPayload.set(e.phase_id, rec(e.payload));
  }
  const byId = new Map(sorted.map((p) => [p.phase_id, p]));
  for (const t of tries) {
    let last: Phase | null = null;
    for (let i = t.own.length - 1; i >= 0; i--) {
      const p = byId.get(t.own[i]);
      if (!p || p.status === "queued") continue;
      last = p;
      break;
    }
    if (!last) continue;
    if (!errorPhases.has(last.phase_id) && last.status !== "fail") continue;
    t.endedBy = last.phase_id;
    const payload = errorPayload.get(last.phase_id) ?? null;
    let reason: string | null = null;
    const fromReason = payload?.reason;
    const fromError = payload?.error;
    if (typeof fromReason === "string" && fromReason.trim() !== "") {
      reason = fromReason;
    } else if (typeof fromError === "string" && fromError.trim() !== "") {
      reason = fromError;
    } else if (last.error && last.error.trim() !== "") {
      reason = last.error;
    }
    if (reason !== null) {
      const cut = reason.split(":")[0].trim();
      reason = cut === "" ? null : cut;
    }
    t.reason = reason;
  }

  // 6. a try failed when it was ended by a failure, or when a later try exists
  for (const t of tries) {
    t.failed = t.endedBy !== null || t.n < tries.length;
  }

  return tries;
}

// ── backbone: one spine card per planned step, retries as branches ───────────

export interface Branch {
  /** The branch's first run's phase_id. */
  key: string;
  try: number;
  anchor: string;
  runs: string[];
}

export interface StepFinal {
  run: string;
  tries: number;
  count: number;
}

export interface Backbone {
  tries: RunTry[];
  spine: string[];
  branches: Branch[];
  final: Record<string, StepFinal>;
  tryOf: Record<string, number>;
  branchOf: Record<string, string>;
  ord: Record<string, number>;
  ended: Set<string>;
  invalidated: Set<string>;
}

/**
 * The Run's backbone: the planned steps as one spine card each, every other run
 * folded into a branch under the card it re-runs. A revise is never a step; a
 * resumed try that re-runs spine steps before reaching a new one hangs under
 * the step where the previous try ended; a build retry hangs under its build
 * card; anything else hangs under the last spine card the try reached.
 */
export function backbone(
  phases: Phase[],
  events: FeedEvent[] = [],
  run: Pick<Run, "started_at" | "engineer"> | null = null,
): Backbone {
  const tries = runTries(phases, events, run);
  const sorted = bySeq(phases);
  const byId = new Map(sorted.map((p) => [p.phase_id, p]));
  const seqOf = new Map<string, number>();
  for (const p of sorted) seqOf.set(p.phase_id, p.seq ?? Infinity);

  // spine: the first run of each step name, in seq order
  const seen = new Set<string>();
  const spinePhases: Phase[] = [];
  for (const p of sorted) {
    const step = stepName(p);
    if (REVISE.test(step) || seen.has(step)) continue;
    seen.add(step);
    spinePhases.push(p);
  }
  if (spinePhases.length === 0 && sorted.length > 0) spinePhases.push(...sorted);

  const spine = spinePhases.map((p) => p.phase_id);
  const onSpine = new Set(spine);
  const spineOfStep = new Map<string, string>();
  for (const p of spinePhases) {
    const step = stepName(p);
    if (!spineOfStep.has(step)) spineOfStep.set(step, p.phase_id);
  }

  // tryOf: which try owns each phase
  const tryOf: Record<string, number> = {};
  for (const t of tries) for (const id of t.own) tryOf[id] = t.n;

  // branches
  const branches: Branch[] = [];
  for (const t of tries) {
    let reached: string | null = null;
    let cur: Branch | null = null;
    for (const id of t.own) {
      const p = byId.get(id);
      if (!p) continue;
      if (onSpine.has(id)) {
        reached = id;
        cur = null;
        continue;
      }
      const step = stepName(p);
      if (!cur) {
        let anchor: string | null = null;
        if (!reached && t.n > 1) {
          const prevId = tries[t.n - 2].endedBy;
          const prev = prevId ? byId.get(prevId) ?? null : null;
          const prevStep = prev
            ? REVISE.test(stepName(prev))
              ? "review"
              : stepName(prev)
            : step;
          anchor = spineOfStep.get(prevStep) ?? null;
        } else if (BUILD_STEP.test(step)) {
          anchor = spineOfStep.get(step) ?? null;
        }
        if (!anchor) anchor = reached;
        if (!anchor) anchor = spineOfStep.get(step) ?? null;
        if (!anchor) {
          const seq = p.seq ?? Infinity;
          for (const s of spinePhases) {
            if ((s.seq ?? Infinity) < seq) anchor = s.phase_id;
          }
        }
        if (!anchor) anchor = spine[0] ?? null;
        cur = { key: id, try: t.n, anchor: anchor ?? id, runs: [] };
        branches.push(cur);
      }
      cur.runs.push(id);
    }
  }

  // branchOf: the branch each folded run belongs to
  const branchOf: Record<string, string> = {};
  for (const b of branches) for (const id of b.runs) branchOf[id] = b.key;

  // ord: the k-th run of its step within its try (reused runs counted too)
  const ord: Record<string, number> = {};
  for (const t of tries) {
    const own = new Set(t.own);
    const all = [...t.reused, ...t.own].sort(
      (a, b) => (seqOf.get(a) ?? Infinity) - (seqOf.get(b) ?? Infinity),
    );
    const seenStep: Record<string, number> = {};
    for (const id of all) {
      const p = byId.get(id);
      if (!p) continue;
      const step = stepName(p);
      const k = (seenStep[step] ?? 0) + 1;
      seenStep[step] = k;
      if (own.has(id)) ord[id] = k;
    }
  }

  // final: a spine card's outcome, read from its branches
  const final: Record<string, StepFinal> = {};
  for (const s of spinePhases) {
    const sid = s.phase_id;
    const on = branches.filter((b) => b.anchor === sid);
    if (on.length === 0) continue;
    const step = stepName(s);
    const runs: string[] = [];
    for (const b of on) for (const id of b.runs) runs.push(id);
    const matching = runs
      .filter((id) => {
        const p = byId.get(id);
        return p ? stepName(p) === step : false;
      })
      .sort((a, b) => (seqOf.get(a) ?? Infinity) - (seqOf.get(b) ?? Infinity));
    const from = new Set<number>();
    from.add(tryOf[sid] ?? 1);
    for (const b of on) from.add(b.try);
    final[sid] = {
      run: matching.length > 0 ? matching[matching.length - 1] : sid,
      tries: from.size,
      count: 1 + on.reduce((n, b) => n + b.runs.length, 0),
    };
  }

  const ended = new Set<string>();
  for (const t of tries) if (t.endedBy) ended.add(t.endedBy);

  const invalidated = new Set<string>();
  for (const e of events) {
    if (e.type === "log" && e.name === "phase_invalidated" && e.phase_id) {
      invalidated.add(e.phase_id);
    }
  }

  return {
    tries,
    spine,
    branches,
    final,
    tryOf,
    branchOf,
    ord,
    ended,
    invalidated,
  };
}

interface LayoutRow {
  stage: CanvasStage;
  first: boolean;
  items: Phase[];
  y: number;
  right: number;
  branched: boolean;
}

export function layoutCanvas(
  phases: Phase[],
  tree: Backbone = backbone(phases),
  open: ReadonlySet<number> = new Set(),
): CanvasLayout {
  if (phases.length === 0) {
    return {
      pos: {},
      labels: [],
      edges: [],
      pills: [],
      width: 0,
      height: 0,
      bounds: { x: 0, y: 0, w: 0, h: 0 },
    };
  }

  const sorted = bySeq(phases);
  const byId = new Map(sorted.map((p) => [p.phase_id, p]));
  const spinePhases = tree.spine
    .map((id) => byId.get(id))
    .filter((p): p is Phase => !!p);
  // Only spine phases sit in the stage rows; a tree with no spine (defensive)
  // falls back to every phase, in seq order.
  const order = spinePhases.length > 0 ? spinePhases : sorted;

  const rows: LayoutRow[] = [];
  const rowOrder: readonly CanvasStage[] = [...STAGE_ROWS, OTHER_ROW];
  for (const stage of rowOrder) {
    const group = order.filter((p) => stageRow(p.stage) === stage);
    for (let i = 0; i < group.length; i += MAX_COLS) {
      rows.push({
        stage,
        first: i === 0,
        items: group.slice(i, i + MAX_COLS),
        y: 0,
        right: 0,
        branched: false,
      });
    }
  }

  const pos: Record<string, CardPos> = {};
  const labels: StageLabel[] = [];
  const edges: CanvasEdge[] = [];
  const pills: CanvasPill[] = [];

  const tryByN = new Map(tree.tries.map((t) => [t.n, t]));
  const firstRevise = (b: Branch): number => {
    const i = b.runs.findIndex((id) => {
      const p = byId.get(id);
      return p ? REVISE.test(stepName(p)) : false;
    });
    return i < 0 ? 0 : i;
  };

  let y = PAD_T;
  rows.forEach((row, ri) => {
    row.y = y;
    if (row.first) labels.push({ text: row.stage, x: PAD_X + 2, y: y - 26 });
    row.items.forEach((p, ci) => {
      pos[p.phase_id] = {
        x: PAD_X + ci * (CARD_W + GAP_X),
        y,
        row: ri,
        w: CARD_W,
        h: CARD_H,
        mini: false,
      };
    });
    row.right = PAD_X + row.items.length * (CARD_W + GAP_X) - GAP_X;

    const inRow = new Set(row.items.map((p) => p.phase_id));
    const anchored = tree.branches.filter((b) => inRow.has(b.anchor));
    if (anchored.length === 0) {
      y += CARD_H + GAP_Y;
      return;
    }

    const pillTop = y + CARD_H + 34;
    const pillC = pillTop + 13;
    let bottom = pillTop + PILL_H;
    let colX = -Infinity;

    // anchors in row order, each with its branches in try order
    for (const p of row.items) {
      const mine = anchored
        .filter((b) => b.anchor === p.phase_id)
        .sort((a, b) => a.try - b.try);
      if (mine.length === 0) continue;

      const a = pos[p.phase_id];
      const tx = a.x + 14;
      const xs = mine.map((_, i) => Math.max(a.x + 34, colX) + i * TRY_PITCH);

      const trunkDone = mine.some((b) => {
        const t = tryByN.get(b.try);
        return t ? t.failed === false : false;
      });
      edges.push({
        id: `trunk:${p.phase_id}`,
        d:
          `M${tx} ${a.y + CARD_H + 6} V${pillC - 12} ` +
          `Q${tx} ${pillC} ${tx + 12} ${pillC} H${xs[xs.length - 1] - 4}`,
        head: "",
        done: trunkDone,
        flowing: false,
        broken: false,
        trunk: true,
      });

      const openMine = mine.filter((b) => open.has(b.try));
      const lead = Math.max(0, ...openMine.map((b) => firstRevise(b)));

      mine.forEach((b, i) => {
        const x = xs[i];
        const isOpen = open.has(b.try);
        pills.push({ key: b.key, try: b.try, x, y: pillC, open: isOpen });
        if (!isOpen) return;

        const cx = x + 10;
        const top =
          pillTop +
          PILL_H +
          MINI_GAP +
          (lead - firstRevise(b)) * (MINI_H + MINI_GAP);
        const mid = cx + MINI_W / 2;
        const t = tryByN.get(b.try);

        b.runs.forEach((id, j) => {
          pos[id] = {
            x: cx,
            y: top + j * (MINI_H + MINI_GAP),
            row: ri,
            w: MINI_W,
            h: MINI_H,
            mini: true,
          };
        });

        edges.push({
          id: `drop:${b.key}`,
          d: `M${mid} ${pillTop + 28}V${top - 7}`,
          head: `M${mid - 4.5} ${top - 12}L${mid} ${top - 6}L${mid + 4.5} ${top - 12}`,
          done: t ? t.failed === false : false,
          flowing: false,
          broken: tree.ended.has(b.runs[0]),
          trunk: false,
        });

        for (let j = 1; j < b.runs.length; j++) {
          const y1 = top + (j - 1) * (MINI_H + MINI_GAP) + MINI_H;
          const y2 = top + j * (MINI_H + MINI_GAP);
          const prev = byId.get(b.runs[j - 1]);
          edges.push({
            id: b.runs[j],
            d: `M${mid} ${y1 + 4}V${y2 - 7}`,
            head: `M${mid - 4.5} ${y2 - 12}L${mid} ${y2 - 6}L${mid + 4.5} ${y2 - 12}`,
            done: prev ? prev.status === "success" : false,
            flowing: false,
            broken: tree.ended.has(b.runs[j]),
            trunk: false,
          });
        }

        bottom = Math.max(
          bottom,
          top + b.runs.length * (MINI_H + MINI_GAP) - MINI_GAP,
        );
      });

      colX = xs[xs.length - 1] + TRY_PITCH;
      row.right = Math.max(row.right, colX - 20);
      row.branched = true;
    }

    y = bottom + GAP_Y;
  });

  // the spine's own connectors, along the same row or wrapped
  for (let i = 1; i < tree.spine.length; i++) {
    const a = byId.get(tree.spine[i - 1]);
    const b = byId.get(tree.spine[i]);
    if (!a || !b) continue;
    const pa = pos[a.phase_id];
    const pb = pos[b.phase_id];
    if (!pa || !pb) continue;

    const ya = pa.y + CARD_H / 2;
    const yb = pb.y + CARD_H / 2;
    let d: string;
    if (pa.row === pb.row) {
      d = `M${pa.x + CARD_W + 6} ${ya}H${pb.x - 9}`;
    } else {
      const r = 12;
      const aRow = rows[pa.row];
      const bRow = rows[pb.row];
      // a wrap off a branched row runs down the right of its columns
      const xr = aRow?.branched ? aRow.right + 30 : pa.x + CARD_W + 30;
      const ym = aRow?.branched ? (bRow?.y ?? pb.y) - 34 : pa.y + CARD_H + 34;
      const xl = pb.x - 30;
      const h = xl < xr ? -1 : 1;
      const s = yb > ym ? 1 : -1;
      d =
        `M${pa.x + CARD_W + 6} ${ya} H${xr - r} ` +
        `Q${xr} ${ya} ${xr} ${ya + r} ` +
        `V${ym - r} Q${xr} ${ym} ${xr + h * r} ${ym} ` +
        `H${xl - h * r} Q${xl} ${ym} ${xl} ${ym + s * r} ` +
        `V${yb - s * r} Q${xl} ${yb} ${xl + r} ${yb} ` +
        `H${pb.x - 9}`;
    }

    const head = `M${pb.x - 14} ${yb - 4.5}L${pb.x - 7} ${yb}L${pb.x - 14} ${yb + 4.5}`;
    edges.push({
      id: b.phase_id,
      d,
      head,
      done: a.status === "success",
      flowing: b.status === "running",
      broken: b.status === "fail" || tree.ended.has(b.phase_id),
      trunk: false,
    });
  }

  const width =
    rows.reduce((w, r) => Math.max(w, r.right + (r.branched ? 40 : 0)), 0) +
    PAD_X;
  const height = y - GAP_Y + PAD_B;
  const bounds: Rect = {
    x: PAD_X - 34,
    y: PAD_T - 30,
    w: width - 2 * PAD_X + 68,
    h: height - PAD_T - PAD_B + 38,
  };

  return { pos, labels, edges, pills, width, height, bounds };
}

/** The context bar fills toward the Session's `budget_warn`, never a fixed
 *  number. Null unless both a positive usage and a positive warning exist. */
export function contextBar(
  used: number | null | undefined,
  warn: number | null | undefined,
): { used: number; warn: number; pct: number; hot: boolean } | null {
  if (typeof used !== "number" || !Number.isFinite(used) || used <= 0) return null;
  if (typeof warn !== "number" || !Number.isFinite(warn) || warn <= 0) return null;
  const pct = Math.min(100, (used / warn) * 100);
  return { used, warn, pct, hot: pct > 80 };
}

function rec(p: unknown): Record<string, unknown> | null {
  if (p && typeof p === "object" && !Array.isArray(p)) {
    return p as Record<string, unknown>;
  }
  return null;
}

/** The live action for one phase's events, in feed order: the tool of the last
 *  call not yet closed by its `tool_result`. `agent_start` clears the map. */
export function liveAction(events: FeedEvent[]): string {
  const open = new Map<string, string>();
  for (const e of events) {
    if (e.type === "agent_start") {
      open.clear();
      continue;
    }
    const p = rec(e.payload);
    if (e.type === "tool_call") {
      const id = p?.tool_call_id;
      if (typeof id === "string") {
        const tool =
          e.name || (typeof p?.tool === "string" ? p.tool : "");
        open.set(id, tool);
      }
    } else if (e.type === "tool_result") {
      const id = p?.tool_call_id;
      if (typeof id === "string") open.delete(id);
    }
  }
  let last: string | undefined;
  for (const tool of open.values()) last = tool;
  return `${last ?? "Thinking"}…`;
}

/** The first `tool_call` or `log` event whose payload has a string `command`. */
export function firstCommand(events: FeedEvent[]): string | null {
  for (const e of events) {
    if (e.type !== "tool_call" && e.type !== "log") continue;
    const c = rec(e.payload)?.command;
    if (typeof c === "string") return c;
  }
  return null;
}

export interface CanvasCard {
  id: string;
  /** The raw `p.name` (the detail tab and the tooltip read it). */
  name: string;
  /** The step name: the raw name with its final `_retryN` stripped. */
  label: string;
  status: "queued" | "running" | "success" | "fail";
  mark: "success" | "fail" | "blocked" | "running" | "queued";
  /** The time slot's colour. */
  tone: "approved" | "blocked" | "ended" | null;
  verdict: "approved" | "blocked" | null;
  /** This run ended its try. */
  ended: boolean;
  /** The k-th run of its step within its try. */
  ord: number;
  /** A spine card's `N tries` / `N runs` note, else null. */
  note: string | null;
  /** The branch card's second line. */
  sub: string;
  icon: string;
  ownerLine: string;
  model: string | null;
  ctx: ReturnType<typeof contextBar>;
  time: string;
  live: boolean;
  warn: boolean;
  fresh: boolean;
}

interface Review {
  verdict: "approved" | "blocked";
  blocking: number;
}

/** The phase's latest valid `ReviewOutput` envelope, by `(attempt, envelope_id)`.
 *  A malformed payload or a non-boolean `approved` gives null, never a throw. */
function reviewOf(envelopes: Envelope[], phaseId: string): Review | null {
  let best: Envelope | null = null;
  for (const e of envelopes) {
    if (e.phase_id !== phaseId) continue;
    if (e.output_type !== "ReviewOutput" || !e.valid) continue;
    if (
      !best ||
      (e.attempt ?? 0) > (best.attempt ?? 0) ||
      ((e.attempt ?? 0) === (best.attempt ?? 0) && e.envelope_id > best.envelope_id)
    ) {
      best = e;
    }
  }
  if (!best) return null;
  const payload = parsePayload(best.payload_json);
  if (!payload || typeof payload.approved !== "boolean") return null;
  return {
    verdict: payload.approved ? "approved" : "blocked",
    blocking: Array.isArray(payload.blocking) ? payload.blocking.length : 0,
  };
}

export function cardsOf(
  phases: Phase[],
  sessions: Session[],
  events: FeedEvent[],
  nowMs: number,
  engineer: string | null = null,
  envelopes: Envelope[] = [],
  tree: Backbone = backbone(phases, events),
): CanvasCard[] {
  const byPhase = new Map<string, FeedEvent[]>();
  for (const e of events) {
    if (!e.phase_id) continue;
    const arr = byPhase.get(e.phase_id);
    if (arr) arr.push(e);
    else byPhase.set(e.phase_id, [e]);
  }

  const own = new Map<string, CanvasCard>();
  const sorted = bySeq(phases);
  for (const p of sorted) {
    const id = p.phase_id;
    const phaseEvents = byPhase.get(id) ?? [];
    const session = sessions.findLast((s) => s.phase_id === id) ?? null;
    const status = p.status ?? "queued";

    let icon: string;
    let ownerLine: string;
    let model: string | null = null;
    let ctx: ReturnType<typeof contextBar> = null;

    if (p.kind === "agent") {
      model = session?.model ?? null;
      icon = modelLogo(model);
      ownerLine = `${p.owner ?? "?"} · ${modelName(model) || "…"}`;
      let latestTurnTokens: number | null = null;
      for (const e of phaseEvents) {
        if (e.type === "turn") latestTurnTokens = e.tokens;
      }
      ctx = contextBar(
        latestTurnTokens ?? session?.context_tokens,
        session?.budget_warn,
      );
    } else if (p.kind === "code") {
      icon = commandIcon(firstCommand(phaseEvents));
      ownerLine = "spine · python";
    } else {
      icon = LIME_ICON;
      ownerLine = engineer ?? "engineer";
    }

    let time: string;
    let live = false;
    if (status === "running" && p.kind === "agent") {
      time = liveAction(phaseEvents);
      live = true;
    } else if (status === "queued") {
      time = "queued";
    } else {
      time = fmtDuration(p.started_at, p.ended_at, nowMs);
    }

    const warn = phaseEvents.some((e) => e.type === "budget_warn");
    const fresh =
      phaseEvents.some((e) => e.type === "continuation") ||
      sessions.some((s) => s.phase_id === id && (s.continuation ?? 0) > 0);

    const review = reviewOf(envelopes, id);
    const verdict = review?.verdict ?? null;
    const dur = fmtDuration(p.started_at, p.ended_at, nowMs);
    const vtext = review
      ? review.verdict === "approved"
        ? `approved · ${dur}`
        : `${review.blocking} blocking · ${dur}`
      : null;
    if (vtext && status !== "running" && status !== "queued") time = vtext;

    const ended = tree.ended.has(id);
    let mark: CanvasCard["mark"];
    if (status === "running") mark = "running";
    else if (status === "queued") mark = "queued";
    else if (ended) mark = "fail";
    else if (verdict === "blocked") mark = "blocked";
    else if (status === "success") mark = "success";
    else mark = "fail";

    const tone: CanvasCard["tone"] =
      verdict === "approved" ? "approved" : verdict === "blocked" ? "blocked" : null;

    let who: string;
    if (p.kind === "code") who = "spine";
    else if (p.kind === "agent") who = p.owner ?? "?";
    else who = engineer ?? "engineer";

    const card: CanvasCard = {
      id,
      name: p.name ?? p.phase_id,
      label: stepName(p),
      status,
      mark,
      tone,
      verdict,
      ended,
      ord: tree.ord[id] ?? 1,
      note: null,
      sub: vtext ?? `${who} · ${time}`,
      icon,
      ownerLine,
      model,
      ctx,
      time,
      live,
      warn,
      fresh,
    };
    own.set(id, card);
  }

  const cards = sorted.map((p) => own.get(p.phase_id)!);

  // a spine card whose step branched wears its final run's face
  for (const card of cards) {
    const f = tree.final[card.id];
    if (!f) continue;
    const fr = own.get(f.run);
    if (!fr) continue;
    const n = tree.tryOf[f.run] ?? 1;
    card.time =
      fr.status === "running"
        ? `running in try ${n}`
        : fr.verdict === "approved"
          ? `approved in try ${n}`
          : fr.verdict === "blocked"
            ? `not approved · try ${n}`
            : `${fr.status ?? "queued"} · try ${n}`;
    card.note = f.tries > 1 ? `${f.tries} tries` : `${f.count} runs`;
    card.ctx = null;
    card.mark = fr.mark;
    card.verdict = fr.verdict;
    card.ended = fr.ended;
    card.tone = fr.ended ? "ended" : fr.tone;
    card.live = false;
  }

  return cards;
}

// ── pills: one per try's branch, and the fold rules ──────────────────────────

export interface PillFace {
  title: string;
  flag: string | null;
  time: string;
  size: string;
  verdict: string;
  ok: boolean | null;
  tip: string;
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** The local `HH:MM` of `iso`, `—` when it is not a valid time. */
function clockHM(iso: string | null | undefined): string {
  const t = ts(iso);
  if (!Number.isFinite(t)) return "—";
  const d = new Date(t);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

/** One pill face per branch, keyed by the branch's key. */
export function pillFaces(
  tree: Backbone,
  cards: CanvasCard[],
): Record<string, PillFace> {
  const byId = new Map(cards.map((c) => [c.id, c]));
  const out: Record<string, PillFace> = {};

  for (const b of tree.branches) {
    const t = tree.tries.find((x) => x.n === b.try) ?? null;
    const n = b.try;
    const how = t?.how ?? "resume";
    const who = t?.who ?? null;
    const start = t?.start ?? null;
    const runs = b.runs.map((id) => byId.get(id)).filter((c): c is CanvasCard => !!c);

    const rounds = runs.filter((c) => REVISE.test(c.label)).length;
    const size =
      rounds > 0
        ? `${rounds} ${rounds === 1 ? "round" : "rounds"}`
        : `${b.runs.length} ${b.runs.length === 1 ? "run" : "runs"}`;

    let verdict: string;
    let ok: boolean | null;
    const last = [...runs].reverse().find((c) => c.verdict !== null) ?? null;
    if (last) {
      verdict = last.verdict === "approved" ? "✓ approved" : "✕ not approved";
      ok = last.verdict === "approved";
    } else {
      const mark = runs[runs.length - 1]?.mark ?? "queued";
      if (mark === "running") {
        verdict = "running";
        ok = null;
      } else if (mark === "success") {
        verdict = "✓ passed";
        ok = true;
      } else {
        verdict = "✕ failed";
        ok = false;
      }
    }

    let tip = `try ${n}: ${
      n === 1 ? "lime run" : "--" + how + (who ? " by " + who : "")
    } at ${fmtClock(start)}`;
    const first = runs[0];
    const anchor = byId.get(b.anchor);
    if (first && !REVISE.test(first.label) && first.label !== (anchor?.label ?? "")) {
      tip += `; re-entered at ${first.label}`;
    }
    tip += `; ${b.runs.length} runs`;
    if (t?.endedBy && b.runs.includes(t.endedBy)) {
      tip += `; ended: ${t.reason ?? ""}`;
    }

    out[b.key] = {
      title: `Try ${n}`,
      flag: n === 1 ? null : how,
      time: clockHM(start),
      size,
      verdict,
      ok,
      tip,
    };
  }

  return out;
}

/** The try of the branch holding `selected`; null for a spine id, an unknown
 *  id or null. */
export function tryToOpen(selected: string | null, tree: Backbone): number | null {
  if (!selected) return null;
  for (const b of tree.branches) {
    if (b.runs.includes(selected)) return b.try;
  }
  return null;
}

/** Toggle try `n` open or folded. `clear` is true only when it folds `n`
 *  while `selected` is a branch run of try `n` (the selection is dropped). */
export function toggleTry(
  open: ReadonlySet<number>,
  n: number,
  selected: string | null,
  tree: Backbone,
): { open: Set<number>; clear: boolean } {
  const next = new Set(open);
  if (next.has(n)) {
    next.delete(n);
    return { open: next, clear: tryToOpen(selected, tree) === n };
  }
  next.add(n);
  return { open: next, clear: false };
}

export function nextSelection(
  selected: string | null,
  action: { card: string } | "clear",
): string | null {
  if (action === "clear") return null;
  return action.card === selected ? null : action.card;
}

/** The ordinal word for a run's position in its step (`1st`, `2nd`, `3rd`, …). */
export function ordWord(k: number): string {
  if (k === 1) return "1st";
  if (k === 2) return "2nd";
  if (k === 3) return "3rd";
  return `${k}th`;
}
