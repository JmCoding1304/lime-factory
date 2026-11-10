import { describe, expect, test } from "bun:test";
import type { ModelStat } from "./cost";
import { modelName } from "./models";
import * as mod from "./costPage";
import {
  XLBL_PX,
  slotWidth,
  textWidth,
  valLabel,
  barTitle,
  NOTIONAL_LABEL,
  NOTIONAL_SUB,
  CHART,
  ICON_PX,
  ICON_GAP,
  LABEL_PAD_MIN,
  padBottom,
  chartHeight,
  TABS,
  DEFAULT_TAB,
  FOOTNOTE,
  bars,
  chartMax,
} from "./costPage";

function stat(overrides: Partial<ModelStat>): ModelStat {
  return {
    model: "default",
    sub: false,
    runs: 1,
    wins: 1,
    api: 0,
    notional: 0,
    tokens: 0,
    successRate: 1,
    priced: 1,
    perWin: 1,
    timed: 1,
    medianMs: null,
    minMs: null,
    maxMs: null,
    ...overrides,
  };
}

// The 8 raw model ids from the 7-day window
const MODELS = [
  "claude-fable-5",
  "openrouter/deepseek/deepseek-v4-flash",
  "fable",
  "xai/grok-4.6",
  "claude-opus-5[1m]",
  "claude-fable-5-1",
  "openrouter/qwen/qwen3-coder-flash",
  "openrouter/moonshotai/kimi-k3",
];

describe("rotated x labels fit the bottom pad", () => {
  test("padBottom for the 8 models fits the formula", () => {
    const longest = MODELS.reduce((a, b) =>
      textWidth(modelName(a), XLBL_PX) > textWidth(modelName(b), XLBL_PX) ? a : b,
    );
    const maxW = textWidth(modelName(longest), XLBL_PX);
    const sin55 = Math.sin((55 * Math.PI) / 180);
    const expected = ICON_GAP + ICON_PX - 2 + maxW * sin55 + XLBL_PX;
    expect(padBottom(MODELS)).toBeGreaterThanOrEqual(expected);
  });

  test("chartHeight uses top + plotH + padB", () => {
    const padB = padBottom(MODELS);
    expect(chartHeight(padB)).toBe(CHART.PAD.t + CHART.PLOT_H + padB);
  });

  test("single short model returns LABEL_PAD_MIN", () => {
    expect(padBottom(["a"])).toBe(LABEL_PAD_MIN);
  });

  test("empty list returns LABEL_PAD_MIN", () => {
    expect(padBottom([])).toBe(LABEL_PAD_MIN);
  });

  test("40-char model id still fits the formula", () => {
    const longName = "x".repeat(40);
    const maxW = textWidth(longName, XLBL_PX);
    const sin55 = Math.sin((55 * Math.PI) / 180);
    const expected = ICON_GAP + ICON_PX - 2 + maxW * sin55 + XLBL_PX;
    expect(padBottom([longName])).toBeGreaterThanOrEqual(expected);
  });
});

test("ellipsis helpers are gone", () => {
  expect("xLabel" in mod).toBe(false);
  expect("fitLabel" in mod).toBe(false);
});

describe("TABS", () => {
  test("ids are usage, cost, speed", () => {
    expect(TABS.map((t) => t.id)).toEqual(["usage", "cost", "speed"]);
  });

  test("DEFAULT_TAB is usage", () => {
    expect(DEFAULT_TAB).toBe("usage");
  });

  test("every FOOTNOTE entry is non-empty", () => {
    for (const tab of ["usage", "cost", "speed"] as const) {
      expect(FOOTNOTE[tab].length).toBeGreaterThan(0);
    }
  });
});

describe("bars per tab", () => {
  test("usage: sub ON → value priced, solid api, label ends †", () => {
    const result = bars(
      "usage",
      [stat({ model: "claude-sub", sub: true, api: 0, notional: 3.5, priced: 3.5 })],
      slotWidth(1),
    );
    expect(result[0].value).toBe(3.5);
    expect(result[0].solid).toBe(0);
    expect(result[0].label).toMatch(/†$/);
  });

  test("usage: sub OFF → priced null, ghost", () => {
    const result = bars("usage", [stat({ sub: true, priced: null })], slotWidth(1));
    expect(result[0].value).toBeNull();
    expect(result[0].ghost).toBe("n/a — sub");
  });

  test("usage: API model → solid === value, no dagger", () => {
    const result = bars(
      "usage",
      [stat({ sub: false, api: 1.5, priced: 1.5 })],
      slotWidth(1),
    );
    expect(result[0].solid).toBe(result[0].value as number);
    expect(result[0].label).not.toMatch(/†/);
  });

  test("cost: sub with wins → perWin value, solid 0", () => {
    const result = bars(
      "cost",
      [stat({ model: "sub-wins", sub: true, api: 0, wins: 2, priced: 3.5, perWin: 1.75 })],
      slotWidth(8),
    );
    expect(result[0].value).toBe(1.75);
    expect(result[0].solid).toBe(0);
  });

  test("cost: API with wins → solid === value", () => {
    const result = bars(
      "cost",
      [stat({ sub: false, api: 1, wins: 2, priced: 1, perWin: 0.5 })],
      slotWidth(8),
    );
    expect(result[0].solid).toBe(0.5);
    expect(result[0].value).toBe(0.5);
  });

  test("cost: sub 0-win → solid 0, value priced", () => {
    const result = bars(
      "cost",
      [stat({ sub: true, priced: 0.8, perWin: null, wins: 0 })],
      slotWidth(8),
    );
    expect(result[0].solid).toBe(0);
    expect(result[0].value).toBe(0.8);
  });

  test("cost: API 0-win at slotWidth(8) → label '$0.32 · 0 wins'", () => {
    const result = bars(
      "cost",
      [stat({ sub: false, priced: 0.32, perWin: null, wins: 0 })],
      slotWidth(8),
    );
    expect(result[0].label).toBe("$0.32 · 0 wins");
  });

  test("speed: median, label, whisker", () => {
    const result = bars(
      "speed",
      [stat({ medianMs: 120_000, minMs: 60_000, maxMs: 180_000, timed: 2, runs: 3 })],
      slotWidth(8),
    );
    expect(result[0].value).toBe(120_000);
    expect(result[0].label).toBe("2m 00s");
    expect(result[0].whisker).toEqual({ min: 60_000, max: 180_000 });
  });

  test("speed: null median → ghost, no whisker", () => {
    const result = bars(
      "speed",
      [stat({ medianMs: null, minMs: null, maxMs: null })],
      slotWidth(8),
    );
    expect(result[0].value).toBeNull();
    expect(result[0].ghost).toBe("n/a");
    expect(result[0].whisker).toBeNull();
  });
});

describe("chartMax", () => {
  test("floors at 0.01 on $ tabs", () => {
    expect(chartMax("usage", [{ value: 0, solid: 0, label: "", ghost: "", title: "", model: "m", whisker: null }])).toBe(0.01);
    expect(chartMax("cost", [{ value: 0, solid: 0, label: "", ghost: "", title: "", model: "m", whisker: null }])).toBe(0.01);
  });

  test("floors at 1 on speed tab", () => {
    expect(chartMax("speed", [{ value: 0, solid: 0, label: "", ghost: "", title: "", model: "m", whisker: null }])).toBe(1);
  });

  test("whisker max wins over median", () => {
    const b = { value: 5, solid: 5, label: "", ghost: "", title: "", model: "m", whisker: { min: 1, max: 20 } };
    expect(chartMax("speed", [b])).toBe(20);
  });
});

describe("value label above a bar", () => {
  test("0-win bar keeps ' · 0 wins' when it fits", () => {
    expect(
      valLabel(stat({ priced: 0.32, perWin: null, wins: 0 }), slotWidth(8)),
    ).toBe("$0.32 · 0 wins");
  });

  test("0-win bar drops the suffix when it would not fit", () => {
    expect(valLabel(stat({ priced: 0.32, perWin: null, wins: 0 }), 60)).toBe("$0.32");
  });

  test("sub bar with wins gets the dagger", () => {
    expect(
      valLabel(stat({ priced: 3.14, perWin: 3.14, sub: true }), slotWidth(8)),
    ).toBe("$3.14 †");
  });
});

describe("bar hover title", () => {
  test("carries model, wins/runs and tokens", () => {
    expect(
      barTitle(
        stat({
          model: "openrouter/deepseek/deepseek-v4-flash",
          wins: 17,
          runs: 19,
          tokens: 32_880_000,
        }),
      ),
    ).toBe("openrouter/deepseek/deepseek-v4-flash · 17/19 ok · 32880k tok");
  });
});

describe("toggle copy", () => {
  const words = (s: string) => s.trim().split(/\s+/).length;

  test("label is at most 5 words", () => {
    expect(words(NOTIONAL_LABEL)).toBeLessThanOrEqual(5);
  });

  test("sub-lines are at most 6 words", () => {
    expect(words(NOTIONAL_SUB.on)).toBeLessThanOrEqual(6);
    expect(words(NOTIONAL_SUB.off)).toBeLessThanOrEqual(6);
  });
});