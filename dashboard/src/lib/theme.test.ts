import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { THEME_KEY, cycleTheme, setTheme, theme } from "./theme";

// bun runs every test file in one process — restore the globals we replace.
const lsDesc = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
const docDesc = Object.getOwnPropertyDescriptor(globalThis, "document");

let store: Map<string, string>;
let setItemThrows = false;

beforeEach(() => {
  store = new Map();
  setItemThrows = false;
  const ls = {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => {
      if (setItemThrows) throw new Error("quota exceeded");
      store.set(k, v);
    },
    removeItem: (k: string) => {
      store.delete(k);
    },
  };
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    writable: true,
    value: ls,
  });
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    writable: true,
    value: { documentElement: { dataset: {} as Record<string, string> } },
  });
  // the module captured its initial value at load time
  theme.value = "system";
});

afterEach(() => {
  if (lsDesc) Object.defineProperty(globalThis, "localStorage", lsDesc);
  else delete (globalThis as { localStorage?: unknown }).localStorage;
  if (docDesc) Object.defineProperty(globalThis, "document", docDesc);
  else delete (globalThis as { document?: unknown }).document;
});

describe("setTheme", () => {
  test("dark stores the key, applies the attribute and updates the ref", () => {
    setTheme("dark");
    expect(store.get(THEME_KEY)).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(theme.value).toBe("dark");
  });

  test("system stores the key and clears the attribute", () => {
    setTheme("system");
    expect(store.get(THEME_KEY)).toBe("system");
    expect(document.documentElement.dataset.theme).toBeUndefined();
    expect(theme.value).toBe("system");
  });
});

describe("cycleTheme", () => {
  test("system goes light → dark → system, persisting each value", () => {
    cycleTheme();
    expect(theme.value).toBe("light");
    expect(store.get(THEME_KEY)).toBe("light");
    cycleTheme();
    expect(theme.value).toBe("dark");
    expect(store.get(THEME_KEY)).toBe("dark");
    cycleTheme();
    expect(theme.value).toBe("system");
    expect(store.get(THEME_KEY)).toBe("system");
  });
});

describe("persistence failure", () => {
  test("a throwing setItem still applies in-page and does not throw", () => {
    setItemThrows = true;
    expect(() => setTheme("dark")).not.toThrow();
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(theme.value).toBe("dark");
    expect(store.size).toBe(0);
  });
});

describe("index.html", () => {
  test("its pre-paint script reads the same key", () => {
    const html = readFileSync(join(import.meta.dir, "../../index.html"), "utf8");
    expect(html).toContain("localStorage.getItem('lf-theme')");
  });
});
