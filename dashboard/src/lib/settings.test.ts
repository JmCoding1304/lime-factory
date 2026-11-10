import { describe, expect, test } from "bun:test";
import { createKeymap, type KeyEventLike } from "./keymap";
import {
  SETTINGS_SECTIONS,
  rememberFocus,
  settingsBindings,
  shellEscape,
  type SettingsSection,
} from "./settings";

function evt(o: {
  key?: string;
  code?: string;
  altKey?: boolean;
  metaKey?: boolean;
  ctrlKey?: boolean;
  target?: unknown;
}): KeyEventLike & { preventCount: number; stopCount: number } {
  const e = {
    key: o.key ?? "",
    code: o.code ?? "",
    altKey: o.altKey ?? false,
    metaKey: o.metaKey ?? false,
    ctrlKey: o.ctrlKey ?? false,
    target: o.target ?? null,
    preventCount: 0,
    stopCount: 0,
    preventDefault() {
      e.preventCount += 1;
    },
    stopPropagation() {
      e.stopCount += 1;
    },
  };
  return e;
}

describe("SETTINGS_SECTIONS", () => {
  test("cost, profiles and appearance in order with distinct digit keys", () => {
    expect(SETTINGS_SECTIONS.map((s) => s.id)).toEqual([
      "cost",
      "profiles",
      "appearance",
    ]);
    const keys = SETTINGS_SECTIONS.map((s) => s.key);
    expect(keys).toEqual(["1", "2", "3"]);
    expect(new Set(keys).size).toBe(3);
  });
});

describe("settingsBindings", () => {
  test("1/2/3 from the body select cost/profiles/appearance", () => {
    const picks: SettingsSection[] = [];
    const m = createKeymap();
    m.register(settingsBindings((s) => picks.push(s)));
    for (const [key, id] of [
      ["1", "cost"],
      ["2", "profiles"],
      ["3", "appearance"],
    ] as const) {
      expect(
        m.dispatch(evt({ key, target: { tagName: "BODY" } })),
      ).toBe(true);
      expect(picks.at(-1)).toBe(id);
    }
    expect(picks).toEqual(["cost", "profiles", "appearance"]);
  });

  test("section keys are skipped while typing", () => {
    const picks: SettingsSection[] = [];
    const m = createKeymap();
    m.register(settingsBindings((s) => picks.push(s)));
    const e = evt({ key: "1", target: { tagName: "INPUT" } });
    expect(m.dispatch(e)).toBe(false);
    expect(picks).toEqual([]);
    expect(e.preventCount).toBe(0);
  });
});

describe("shellEscape", () => {
  test("fires from an input; true leaves the event untouched", () => {
    let calls = 0;
    const m = createKeymap();
    m.register([
      shellEscape(() => {
        calls += 1;
        return true;
      }),
    ]);
    const e = evt({ key: "Escape", target: { tagName: "INPUT" } });
    expect(m.dispatch(e)).toBe(true);
    expect(calls).toBe(1);
    // prevent:false — RoleBlock's own @keydown.esc still fires
    expect(e.preventCount).toBe(0);
    expect(e.stopCount).toBe(0);
  });

  test("run returning false leaves the event untouched", () => {
    let calls = 0;
    const m = createKeymap();
    m.register([
      shellEscape(() => {
        calls += 1;
        return false;
      }),
    ]);
    const e = evt({ key: "Escape", target: { tagName: "BODY" } });
    expect(m.dispatch(e)).toBe(false);
    expect(calls).toBe(1);
    expect(e.preventCount).toBe(0);
    expect(e.stopCount).toBe(0);
  });

  test("Esc closes and focus returns", () => {
    let n = 0;
    const fakeDoc = {
      activeElement: { focus() { n += 1; }, isConnected: true },
    };
    const restore = rememberFocus(fakeDoc);
    let closed = false;
    const m = createKeymap();
    m.register([
      shellEscape(() => {
        closed = true;
        restore();
        return true;
      }),
    ]);
    expect(
      m.dispatch(evt({ key: "Escape", target: { tagName: "BODY" } })),
    ).toBe(true);
    expect(closed).toBe(true);
    expect(n).toBe(1);
  });
});

describe("rememberFocus", () => {
  test("skips an element no longer connected", () => {
    let n = 0;
    const restore = rememberFocus({
      activeElement: { focus() { n += 1; }, isConnected: false },
    });
    expect(() => restore()).not.toThrow();
    expect(n).toBe(0);
  });

  test("skips a null activeElement", () => {
    const restore = rememberFocus({ activeElement: null });
    expect(() => restore()).not.toThrow();
  });
});
