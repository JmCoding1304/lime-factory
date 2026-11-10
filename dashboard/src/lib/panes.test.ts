import { describe, expect, test } from "bun:test";
import { comboOf } from "./keymap";
import {
  ALL,
  loadPanes,
  paneBindings,
  savePanes,
  togglePane,
  isAll,
  computeDims,
  type PaneSet,
} from "./panes";

function set(...on: Array<"left" | "middle" | "right">): PaneSet {
  return {
    left: on.includes("left"),
    middle: on.includes("middle"),
    right: on.includes("right"),
  };
}

describe("togglePane", () => {
  test("from All, only the named pane", () => {
    expect(togglePane(ALL, "left")).toEqual(set("left"));
  });

  test("all three visible again is All", () => {
    expect(togglePane(set("left", "middle"), "right")).toEqual(ALL);
    expect(isAll(togglePane(set("left", "middle"), "right")!)).toBe(true);
  });

  test("the last visible pane is denied", () => {
    expect(togglePane(set("middle"), "middle")).toBeNull();
  });

  test("an already-visible non-last pane flips off", () => {
    expect(togglePane(set("left", "right"), "left")).toEqual(set("right"));
  });

  test("a hidden pane flips on", () => {
    expect(togglePane(set("left"), "middle")).toEqual(set("left", "middle"));
  });
});

describe("computeDims", () => {
  test("(1400, All)", () => {
    const d = computeDims(1400, ALL);
    expect(d.w).toEqual({ left: 272, middle: 724, right: 388 });
    expect(d.tracks).toEqual([280, 724, 396]);
    expect(d.flex).toBe("middle");
  });

  test("(1200, All) shrinks the side panes", () => {
    const d = computeDims(1200, ALL);
    expect(d.w.left).toBe(240);
    expect(d.w.right).toBe(340);
  });

  test("(1400, {left,right})", () => {
    const d = computeDims(1400, set("left", "right"));
    expect(d.flex).toBe("right");
    expect(d.gL).toBe(8);
    expect(d.gR).toBe(0);
    expect(d.w.right).toBe(1400 - 8 - 272);
  });

  test("(1400, {left})", () => {
    const d = computeDims(1400, set("left"));
    expect(d.flex).toBe("left");
    expect(d.w.left).toBe(1400);
    expect(d.gL).toBe(0);
  });
});

function fakeStorage(init: Record<string, string> = {}) {
  const map = new Map<string, string>(Object.entries(init));
  return {
    map,
    getItem: (k: string) => (map.has(k) ? map.get(k)! : null),
    setItem: (k: string, v: string) => {
      map.set(k, v);
    },
  };
}

describe("loadPanes", () => {
  test("a known array is honoured", () => {
    const st = fakeStorage({ "lf-layout": '["left","right"]' });
    expect(loadPanes(st)).toEqual(set("left", "right"));
  });

  test("invalid JSON opens All", () => {
    expect(loadPanes(fakeStorage({ "lf-layout": "{not json" }))).toEqual(ALL);
  });

  test("an empty array opens All", () => {
    expect(loadPanes(fakeStorage({ "lf-layout": "[]" }))).toEqual(ALL);
  });

  test("unknown names open All", () => {
    expect(loadPanes(fakeStorage({ "lf-layout": '["bogus"]' }))).toEqual(ALL);
  });

  test("no storage opens All", () => {
    expect(loadPanes(null)).toEqual(ALL);
  });

  test("a throwing getItem opens All", () => {
    const st = {
      getItem() {
        throw new Error("nope");
      },
    };
    expect(loadPanes(st)).toEqual(ALL);
  });
});

describe("savePanes", () => {
  test("writes visible names in PANES order", () => {
    const st = fakeStorage();
    savePanes(st, set("left", "middle"));
    expect(st.map.get("lf-layout")).toBe('["left","middle"]');
  });

  test("a throwing setItem is swallowed", () => {
    const st = {
      setItem() {
        throw new Error("nope");
      },
    };
    expect(() => savePanes(st, ALL)).not.toThrow();
  });
});

describe("paneBindings", () => {
  test("⌥1–4 by code, all whileTyping", () => {
    const bs = paneBindings({ showAll: () => {}, toggle: () => {} });
    expect(bs.map(comboOf)).toEqual([
      "alt+code:Digit1",
      "alt+code:Digit2",
      "alt+code:Digit3",
      "alt+code:Digit4",
    ]);
    expect(bs.every((b) => b.whileTyping)).toBe(true);
  });
});
