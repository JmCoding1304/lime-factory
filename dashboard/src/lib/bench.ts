// Session-only bench geometry for the profiles canvas — positions, wires,
// pan/zoom. Never part of the config; nothing here is persisted.

import { orderRoles } from "./profiles";

export const GRID = 22;
export const BLOCK_W = 236;
export const BLOCK_H_EST = 150;
export const COLS = 3;
export const COL_PITCH = 302;
export const ROW_PITCH = 198;

export const ORIGIN = { x: 66, y: 66 };
export const ZOOM = { min: 0.25, max: 3 };

export interface Pos {
  x: number;
  y: number;
}
export interface View {
  x: number;
  y: number;
  k: number;
}
export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export const snap = (v: number): number =>
  Math.max(0, Math.round(v / GRID) * GRID);

export const slotPos = (i: number): Pos => ({
  x: ORIGIN.x + (i % COLS) * COL_PITCH,
  y: ORIGIN.y + Math.floor(i / COLS) * ROW_PITCH,
});

/** Place keys without a position on their natural slot, skipping slots where a
 *  placed block would overlap (BLOCK_W+20 × 96 exclusion box). Mutates + returns
 *  the same Map. */
export function placeMissing(
  keys: string[],
  positions: Map<string, Pos>,
): Map<string, Pos> {
  const collides = (i: number): boolean => {
    const p = slotPos(i);
    for (const q of positions.values()) {
      if (
        Math.abs(q.x - p.x) < BLOCK_W + 20 &&
        Math.abs(q.y - p.y) < 96
      ) {
        return true;
      }
    }
    return false;
  };
  keys.forEach((key, index) => {
    if (positions.has(key)) return;
    let i = index;
    while (collides(i)) i++;
    positions.set(key, slotPos(i));
  });
  return positions;
}

/** Consecutive pairs of the bench in chain order (ROLE_ORDER first). */
export function chainPairs(bench: string[]): [string, string][] {
  const roles = orderRoles(bench);
  const pairs: [string, string][] = [];
  for (let i = 0; i < roles.length - 1; i++) {
    pairs.push([roles[i], roles[i + 1]]);
  }
  return pairs;
}

export function wirePath(a: Rect, b: Rect): string {
  if (b.x >= a.x + a.w / 2) {
    const x1 = a.x + a.w;
    const y1 = a.y + a.h / 2;
    const x2 = b.x;
    const y2 = b.y + b.h / 2;
    const dx = Math.max(40, Math.min(120, (x2 - x1) / 2));
    return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
  }
  const x1 = a.x + a.w / 2;
  const y1 = a.y + a.h;
  const x2 = b.x + b.w / 2;
  const y2 = b.y;
  const dy = Math.max(40, Math.min(120, (y2 - y1) / 2));
  return `M ${x1} ${y1} C ${x1} ${y1 + dy}, ${x2} ${y2 - dy}, ${x2} ${y2}`;
}

/** Zoom around the cursor point, clamped to ZOOM. */
export function zoomAt(
  v: View,
  cx: number,
  cy: number,
  factor: number,
): View {
  const k2 = Math.min(ZOOM.max, Math.max(ZOOM.min, v.k * factor));
  return {
    x: cx - ((cx - v.x) * k2) / v.k,
    y: cy - ((cy - v.y) * k2) / v.k,
    k: k2,
  };
}

export const panBy = (v: View, dx: number, dy: number): View => ({
  ...v,
  x: v.x + dx,
  y: v.y + dy,
});

/** Plain wheel pans; ctrl/meta wheel (pinch) zooms around the cursor. */
export function wheelToView(
  v: View,
  e: { deltaX: number; deltaY: number; ctrlKey: boolean; metaKey: boolean },
  cx: number,
  cy: number,
): View {
  if (e.ctrlKey || e.metaKey) {
    return zoomAt(v, cx, cy, Math.exp(-e.deltaY * 0.01));
  }
  return panBy(v, -e.deltaX, -e.deltaY);
}

/** Canvas-relative client coords → stage coords. */
export const toStage = (
  v: View,
  clientX: number,
  clientY: number,
): Pos => ({ x: (clientX - v.x) / v.k, y: (clientY - v.y) / v.k });

/** The view that centres `content` (stage coords) in a `w×h` viewport with
 *  `margin` px kept clear on every side. Never zooms past 1, never below ZOOM.min. */
export function fitView(content: Rect, w: number, h: number, margin = 24): View {
  let k = 1;
  if (content.w > 0 && content.h > 0) {
    const raw = Math.min(
      (w - 2 * margin) / content.w,
      (h - 2 * margin) / content.h,
      1,
    );
    k = Math.min(1, Math.max(ZOOM.min, raw));
  }
  return {
    x: (w - content.w * k) / 2 - content.x * k,
    y: (h - content.h * k) / 2 - content.y * k,
    k,
  };
}
