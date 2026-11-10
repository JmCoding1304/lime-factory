import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import {
  comboOf,
  createKeymap,
  installKeymap,
  type Binding,
  type KeyEventLike,
} from "./keymap";
import { paneBindings } from "./panes";
import { profileBindings } from "./profilesPage";
import { settingsBindings, shellEscape } from "./settings";

const noop = () => {};

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

describe("comboOf", () => {
  test("formats alt then mod then code/key", () => {
    expect(comboOf({ id: "x", code: "Digit2", alt: true, run: noop })).toBe(
      "alt+code:Digit2",
    );
    expect(comboOf({ id: "x", key: "p", mod: true, run: noop })).toBe(
      "mod+key:p",
    );
  });

  test("needs exactly one of code or key", () => {
    expect(() => comboOf({ id: "x", run: noop })).toThrow();
    expect(() =>
      comboOf({ id: "x", code: "Digit2", key: "2", run: noop }),
    ).toThrow();
  });
});

describe("register", () => {
  test("a second binding of the same combo throws", () => {
    const m = createKeymap();
    m.register([{ id: "a", code: "Digit2", alt: true, run: noop }]);
    expect(() =>
      m.register([{ id: "b", code: "Digit2", alt: true, run: noop }]),
    ).toThrow(/duplicate key binding alt\+code:Digit2/);
  });

  test("an internal duplicate adds none of the batch", () => {
    const m = createKeymap();
    expect(() =>
      m.register([
        { id: "uniq", code: "Digit2", alt: true, run: noop },
        { id: "a", code: "Digit3", alt: true, run: noop },
        { id: "b", code: "Digit3", alt: true, run: noop },
      ]),
    ).toThrow();
    expect(m.dispatch(evt({ code: "Digit2", altKey: true, target: { tagName: "BODY" } }))).toBe(
      false,
    );
  });

  test("unregister frees the combo", () => {
    const m = createKeymap();
    const off = m.register([{ id: "a", code: "Digit2", alt: true, run: noop }]);
    off();
    expect(() =>
      m.register([{ id: "b", code: "Digit2", alt: true, run: noop }]),
    ).not.toThrow();
  });

  test("shipped bindings coexist", () => {
    const m = createKeymap();
    expect(() =>
      m.register([
        ...paneBindings({ showAll: noop, toggle: noop }),
        shellEscape(() => false),
        ...settingsBindings(noop),
        ...profileBindings(noop),
      ]),
    ).not.toThrow();
  });

  test("a second Escape binding throws", () => {
    const m = createKeymap();
    m.register([shellEscape(() => false)]);
    expect(() => m.register([{ id: "x", key: "Escape", run: noop }])).toThrow(
      /duplicate key binding key:Escape/,
    );
  });
});

describe("dispatch", () => {
  test("⌥1 calls showAll", () => {
    const m = createKeymap();
    let shows = 0;
    m.register(paneBindings({ showAll: () => { shows += 1; }, toggle: noop }));
    expect(
      m.dispatch(evt({ code: "Digit1", key: "¡", altKey: true, target: { tagName: "BODY" } })),
    ).toBe(true);
    expect(shows).toBe(1);
  });

  test("pane combos run while typing, keeping focus", () => {
    for (const target of [
      { tagName: "INPUT" },
      { tagName: "TEXTAREA" },
      { tagName: "DIV", isContentEditable: true },
    ]) {
      let blurs = 0;
      let focuses = 0;
      const t = { ...target, blur: () => (blurs += 1), focus: () => (focuses += 1) };
      const m = createKeymap();
      let toggles = 0;
      let last = "";
      m.register(
        paneBindings({
          showAll: noop,
          toggle: (k) => {
            toggles += 1;
            last = k;
          },
        }),
      );
      const e = evt({
        code: "Digit3",
        key: "£",
        altKey: true,
        metaKey: false,
        ctrlKey: false,
        target: t,
      });
      expect(m.dispatch(e)).toBe(true);
      expect(toggles).toBe(1);
      expect(last).toBe("middle");
      expect(e.preventCount).toBe(1);
      expect(e.stopCount).toBe(1);
      expect(blurs).toBe(0);
      expect(focuses).toBe(0);
    }
  });

  test("a default binding skips typing targets", () => {
    const m = createKeymap();
    let ran = 0;
    m.register([{ id: "x", key: "p", run: () => { ran += 1; } }]);
    const inInput = evt({ key: "p", target: { tagName: "INPUT" } });
    expect(m.dispatch(inInput)).toBe(false);
    expect(ran).toBe(0);
    expect(inInput.preventCount).toBe(0);
    const inBody = evt({ key: "p", target: { tagName: "BODY" } });
    expect(m.dispatch(inBody)).toBe(true);
    expect(ran).toBe(1);
  });

  test("modifiers match exactly", () => {
    const m = createKeymap();
    let ran = 0;
    m.register([{ id: "x", code: "Digit2", alt: true, run: () => { ran += 1; } }]);
    expect(m.dispatch(evt({ code: "Digit2", target: { tagName: "BODY" } }))).toBe(false);
    expect(
      m.dispatch(evt({ code: "Digit2", altKey: true, metaKey: true, target: { tagName: "BODY" } })),
    ).toBe(false);
    expect(ran).toBe(0);

    const m2 = createKeymap();
    let s = 0;
    m2.register([{ id: "s", code: "KeyS", mod: true, run: () => { s += 1; } }]);
    expect(m2.dispatch(evt({ code: "KeyS", metaKey: true, target: { tagName: "BODY" } }))).toBe(true);
    expect(m2.dispatch(evt({ code: "KeyS", ctrlKey: true, target: { tagName: "BODY" } }))).toBe(true);
    expect(s).toBe(2);
  });

  test("run returning false leaves the event untouched", () => {
    const m = createKeymap();
    m.register([{ id: "x", key: "p", run: () => false }]);
    const e = evt({ key: "p", target: { tagName: "BODY" } });
    expect(m.dispatch(e)).toBe(false);
    expect(e.preventCount).toBe(0);
    expect(e.stopCount).toBe(0);
  });

  test("prevent:false runs without preventing", () => {
    const m = createKeymap();
    let ran = 0;
    m.register([{ id: "x", key: "p", prevent: false, run: () => { ran += 1; } }]);
    const e = evt({ key: "p", target: { tagName: "BODY" } });
    expect(m.dispatch(e)).toBe(true);
    expect(ran).toBe(1);
    expect(e.preventCount).toBe(0);
    expect(e.stopCount).toBe(0);
  });
});

describe("installKeymap", () => {
  test("installs once per target and dispatches into the given map", () => {
    const calls: unknown[][] = [];
    const target = {
      addEventListener(...args: unknown[]) {
        calls.push(args);
      },
    } as unknown as Pick<Window, "addEventListener">;
    const map = createKeymap();
    installKeymap(target, map);
    installKeymap(target, map);
    expect(calls.length).toBe(1);
    expect(calls[0][0]).toBe("keydown");
    expect(calls[0][2]).toBe(true);
    let ran = 0;
    map.register([{ id: "x", key: "p", run: () => { ran += 1; } }]);
    (calls[0][1] as (e: KeyEventLike) => void)(evt({ key: "p", target: { tagName: "BODY" } }));
    expect(ran).toBe(1);
  });

  test("keymap.ts is the only keydown listener", () => {
    const dir = join(import.meta.dir, "..");
    const files = readdirSync(dir, { recursive: true }) as string[];
    const hits = files
      .map((f) => f.split("\\").join("/"))
      .filter((f) => (f.endsWith(".ts") || f.endsWith(".vue")) && !f.endsWith(".test.ts"))
      .filter((f) => /addEventListener\(\s*["']keydown["']/.test(readFileSync(join(dir, f), "utf8")));
    expect(hits).toEqual(["lib/keymap.ts"]);
  });
});

// keep the binding type referenced so unused-import checks stay quiet if ids change
const _binding: Binding["id"] = "shell: left pane";
void _binding;
