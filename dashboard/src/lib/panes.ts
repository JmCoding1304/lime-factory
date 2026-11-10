// Pure pane rules from prototype B's Fixed 2 layout engine.
import type { Binding } from "./keymap";

export const PANES = ["left", "middle", "right"] as const;
export type Pane = (typeof PANES)[number];
export type PaneSet = Record<Pane, boolean>;

export const LAYOUT_KEY = "lf-layout";
export const PANE_MS = 340;
export const PANE_EASE = "cubic-bezier(.32,.72,0,1)";

// Above these widths a pane's content layout is width-invariant, so a staying
// pane is crossfaded rather than animated.
export const SAFE: Record<Pane, number> = { left: 580, middle: 700, right: 700 };
export const GAP = 8;
export const LEFT_MAX = 560;
export const FOCUS_MAX = 680;

export const ALL: PaneSet = { left: true, middle: true, right: true };

export function isAll(p: PaneSet): boolean {
  return PANES.every((k) => p[k]);
}

/** Port of app.js:98-104 — only a non-empty JSON array of known names is accepted. */
export function loadPanes(storage: Pick<Storage, "getItem"> | null): PaneSet {
  try {
    const v = JSON.parse(storage?.getItem(LAYOUT_KEY) || "null");
    if (Array.isArray(v) && v.length && v.every((k) => PANES.includes(k))) {
      return Object.fromEntries(PANES.map((k) => [k, v.includes(k)])) as PaneSet;
    }
  } catch {
    /* a malformed layout opens All */
  }
  return { ...ALL };
}

export function savePanes(
  storage: Pick<Storage, "setItem"> | null,
  p: PaneSet,
): void {
  try {
    storage?.setItem(LAYOUT_KEY, JSON.stringify(PANES.filter((k) => p[k])));
  } catch {
    /* a failed write keeps the in-memory layout */
  }
}

/** From All, only `k`. Hiding the last visible pane is denied (`null`). */
export function togglePane(p: PaneSet, k: Pane): PaneSet | null {
  if (isAll(p)) return { left: false, middle: false, right: false, [k]: true };
  if (p[k]) {
    if (PANES.filter((x) => p[x]).length === 1) return null;
    return { ...p, [k]: false };
  }
  return { ...p, [k]: true };
}

export interface Dims {
  W: number;
  w: Record<Pane, number>;
  gL: number;
  gR: number;
  flex: Pane;
  tracks: number[];
}

/** Exact port of app.js:352-361. */
export function computeDims(W: number, p: PaneSet): Dims {
  const Lf = W > 1250 ? 272 : 240;
  const Rf = W > 1250 ? 388 : 340;
  const flex: Pane = p.middle ? "middle" : p.right ? "right" : "left";
  const w: Record<Pane, number> = { left: 0, middle: 0, right: 0 };
  const gL = p.left && (p.middle || p.right) ? GAP : 0;
  const gR = p.right && p.middle ? GAP : 0;
  if (p.left && flex !== "left") w.left = Lf;
  if (p.right && flex !== "right") w.right = Rf;
  w[flex] = Math.max(0, W - gL - gR - w.left - w.right);
  return { W, w, gL, gR, flex, tracks: [w.left + gL, w.middle, w.right + gR] };
}

/** ⌥1–4 pane controls, matched by code because ⌥ rewrites e.key on macOS. */
export function paneBindings(a: {
  showAll(): void;
  toggle(k: Pane): void;
}): Binding[] {
  return [
    {
      id: "shell: all panes",
      code: "Digit1",
      alt: true,
      whileTyping: true,
      run: () => a.showAll(),
    },
    {
      id: "shell: left pane",
      code: "Digit2",
      alt: true,
      whileTyping: true,
      run: () => a.toggle("left"),
    },
    {
      id: "shell: middle pane",
      code: "Digit3",
      alt: true,
      whileTyping: true,
      run: () => a.toggle("middle"),
    },
    {
      id: "shell: right pane",
      code: "Digit4",
      alt: true,
      whileTyping: true,
      run: () => a.toggle("right"),
    },
  ];
}
