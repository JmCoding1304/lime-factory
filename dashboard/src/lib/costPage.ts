import { fmtUsd } from "./cost";
import type { ModelStat } from "./cost";
import { fmtK, fmtMs } from "./format";
import { modelName } from "./models";

/** Chart geometry in viewBox units. W≈rendered width at the page's 1060px cap, so font sizes below are real pixels. */
export const CHART = { W: 1000, PLOT_H: 236, PAD: { l: 52, r: 16, t: 16 } } as const;
/** JetBrains Mono advance width is 0.6em; the slack covers the ellipsis glyph. */
export const MONO_EM = 0.62;
export const XLBL_PX = 10.5;
export const VAL_PX = 11;
export const SLOT_GUTTER = 6;

export const slotWidth = (n: number) => (CHART.W - CHART.PAD.l - CHART.PAD.r) / Math.max(1, n);
export const textWidth = (text: string, px: number) => text.length * px * MONO_EM;

export const ICON_PX = 18;
export const ICON_GAP = 6;
export const LABEL_ROT = -55;
export const LABEL_PAD_MIN = 64;

/** Bottom padding that fits the longest rotated x label.
 *  The rotated label hangs down-left from its end anchor, so its vertical
 *  reach is `width · sin 55°`. */
export function padBottom(models: string[]): number {
  if (models.length === 0) return LABEL_PAD_MIN;
  const maxW = Math.max(...models.map((m) => textWidth(modelName(m), XLBL_PX)));
  const sin55 = Math.sin((55 * Math.PI) / 180);
  return Math.max(LABEL_PAD_MIN, Math.ceil(ICON_GAP + ICON_PX - 2 + maxW * sin55 + XLBL_PX));
}

export const chartHeight = (padB: number) => CHART.PAD.t + CHART.PLOT_H + padB;
export const BASE_Y = CHART.PAD.t + CHART.PLOT_H;
export const yOf = (max: number) => (v: number) => CHART.PAD.t + (1 - v / max) * CHART.PLOT_H;

/** Text inside a bar. The 0-win suffix is kept only while it fits the slot. */
export function valLabel(m: ModelStat, slotW: number): string {
  if (m.priced === null) return "n/a — sub";
  if (m.perWin !== null) return fmtUsd(m.perWin) + (m.sub ? " †" : "");
  const long = `${fmtUsd(m.priced)} · 0 wins`;
  return textWidth(long, VAL_PX) <= slotW - SLOT_GUTTER ? long : fmtUsd(m.priced);
}

/** Hover text carrying what used to be the chart's second x-axis line. */
export const barTitle = (m: ModelStat) =>
  `${m.model} · ${m.wins}/${m.runs} ok · ${fmtK(m.tokens)} tok`;

export const NOTIONAL_LABEL = "Price subscription at API rate";
export const NOTIONAL_SUB = {
  on: "on · sub hatched, marked †",
  off: "off · sub excluded from ranking",
} as const;

// ── Tabs ──────────────────────────────────────────────────────────────────────

export type TabId = "usage" | "cost" | "speed";
export const DEFAULT_TAB: TabId = "usage";

export const TABS: { id: TabId; label: string; caption: string; aria: string }[] = [
  { id: "usage", label: "API usage", caption: "api usage $ · by model", aria: "API usage by model" },
  { id: "cost", label: "$ per win", caption: "$ per successful run · by model", aria: "Cost per win by model" },
  { id: "speed", label: "Speed per task", caption: "median task duration · by model", aria: "Speed per task by model" },
];

export const FOOTNOTE: Record<TabId, string> = {
  usage: "solid = real API $ · hatched = notional (subscription priced at API rate)",
  cost: "† notional — subscription turns priced at API rate · solid = real API $ · hatched = notional",
  speed: "bar = median · whiskers = min→max session duration · n/a = no agent_end recorded",
};

// ── Bars ─────────────────────────────────────────────────────────────────────

export interface Bar {
  model: string;
  value: number | null;          // bar top; null → dashed ghost
  solid: number;                 // top of the real-$ segment; hatch overlays solid→value
  label: string;                 // in-bar text
  ghost: string;                 // ghost text when value is null
  title: string;                 // hover
  whisker: { min: number; max: number } | null;
}

export function bars(tab: TabId, stats: ModelStat[], slotW: number): Bar[] {
  return stats.map((m) => {
    switch (tab) {
      case "usage": {
        const value = m.priced;
        const solid = m.sub ? m.api : (m.priced ?? 0);
        const label = m.priced !== null ? fmtUsd(m.priced) + (m.sub ? " †" : "") : "";
        const ghost = m.priced === null ? "n/a — sub" : "";
        return {
          model: m.model,
          value,
          solid,
          label,
          ghost,
          title: barTitle(m),
          whisker: null,
        };
      }
      case "cost": {
        const value = m.priced === null ? null : (m.perWin ?? m.priced);
        let solid: number;
        if (m.wins > 0) {
          solid = m.sub ? m.api / m.wins : (value ?? 0);
        } else {
          solid = m.sub ? 0 : (value ?? 0);
        }
        const label = valLabel(m, slotW);
        const ghost = m.priced === null ? "n/a — sub" : "";
        return {
          model: m.model,
          value,
          solid,
          label,
          ghost,
          title: barTitle(m),
          whisker: null,
        };
      }
      case "speed": {
        const value = m.medianMs;
        const solid = value ?? 0;
        const label = m.medianMs !== null ? fmtMs(m.medianMs) : "";
        const ghost = m.medianMs === null ? "n/a" : "";
        const title = `${m.model} · median ${fmtMs(m.medianMs ?? 0)} · min ${fmtMs(m.minMs ?? 0)} · max ${fmtMs(m.maxMs ?? 0)} · ${m.timed}/${m.runs} timed`;
        const whisker =
          m.minMs !== null && m.maxMs !== null
            ? { min: m.minMs, max: m.maxMs }
            : null;
        return {
          model: m.model,
          value,
          solid,
          label,
          ghost,
          title,
          whisker,
        };
      }
    }
  });
}

export function chartMax(tab: TabId, bs: Bar[]): number {
  const floor = tab === "speed" ? 1 : 0.01;
  let max = 0;
  for (const b of bs) {
    if (b.value !== null && b.value > max) max = b.value;
    if (b.whisker !== null && b.whisker.max > max) max = b.whisker.max;
  }
  return Math.max(max, floor);
}

export const yLabel = (tab: TabId, v: number) =>
  tab === "speed" ? fmtMs(v) : fmtUsd(v);