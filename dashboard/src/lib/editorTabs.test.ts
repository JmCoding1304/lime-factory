import { afterAll, afterEach, beforeAll, describe, expect, test } from "bun:test";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { JSDOM } from "jsdom";
import { isReactive, nextTick, reactive, watchEffect } from "vue";
import { EditorState, Text } from "@codemirror/state";
import { ensureSyntaxTree } from "@codemirror/language";
import { highlightTree } from "@lezer/highlight";
import { undo } from "@codemirror/commands";
import { findNext, openSearchPanel, searchPanelOpen } from "@codemirror/search";
import { EditorView, keymap } from "@codemirror/view";
import { createKeymap, type KeyEventLike } from "./keymap";
import { paneBindings } from "./panes";
import { settingsBindings, shellEscape } from "./settings";
import { FileApiError } from "./api";
import {
  activeTab, activateTab, calmHighlight, canPreview, closeVerdict, createTabState, editorBindings,
  closeRunDock, dockActive, escapeDock, findTab, isUnder, languageOf, movedPath, newRunDock,
  fileTabId, openRunTab, openTab, removeTab, renameTabs, runTabName, runTabPath, saveStateOf, saveTab,
  showPhaseDock, tabsUnder, tabId, toggleMode, toggleRunDock,
  type FileTab, type Lang, type RunDock, type TabStrips,
} from "./editorTabs";

const noop = () => {};

function spans(text: string, path: string): Array<[string, string]> {
  const state = createTabState(text, path, noop);
  const tree = ensureSyntaxTree(state, state.doc.length, 5000);
  if (!tree) return [];
  const out: Array<[string, string]> = [];
  highlightTree(tree, calmHighlight, (f, to, cls) => out.push([text.slice(f, to), cls]));
  return out;
}

function classOf(text: string, path: string, token: string): string | undefined {
  return spans(text, path).find(([s]) => s === token)?.[1];
}

function evt(o: {
  key?: string; code?: string; altKey?: boolean; metaKey?: boolean;
  ctrlKey?: boolean; target?: unknown;
}): KeyEventLike & { preventCount: number; stopCount: number } {
  const e = {
    key: o.key ?? "", code: o.code ?? "", altKey: o.altKey ?? false,
    metaKey: o.metaKey ?? false, ctrlKey: o.ctrlKey ?? false, target: o.target ?? null,
    preventCount: 0, stopCount: 0,
    preventDefault() { e.preventCount += 1; },
    stopPropagation() { e.stopCount += 1; },
  };
  return e;
}

function tab(overrides: Partial<FileTab> = {}): FileTab {
  return {
    id: "p\u0000a.md", project: "p", path: "a.md", name: "a.md", mode: "edit",
    status: "ready", dirty: true, saving: false, error: null, ...overrides,
  };
}

describe("bun.lock", () => {
  test("resolves exactly one @codemirror/state", () => {
    const lock = readFileSync(join(import.meta.dir, "../../bun.lock"), "utf8");
    expect(lock.match(/\["@codemirror\/state@/g)?.length).toBe(1);
  });
});

describe("tab model", () => {
  test("open creates an active loading tab named by its basename", () => {
    const strips: TabStrips = {};
    const { tab: t, created } = openTab(strips, "p", "a/md/README.md");
    expect(created).toBe(true);
    expect(t.name).toBe("README.md");
    expect(t.mode).toBe("edit");
    expect(t.status).toBe("loading");
    expect(strips.p.active).toBe(t.id);
    expect(activeTab(strips, "p")).toBe(t);
  });

  test("re-opening the same path reuses and activates the tab", () => {
    const strips: TabStrips = {};
    openTab(strips, "p", "a.md");
    openTab(strips, "p", "b.md");
    const { tab: t, created } = openTab(strips, "p", "a.md");
    expect(created).toBe(false);
    expect(strips.p.tabs.length).toBe(2);
    expect(strips.p.active).toBe(t.id);
    expect(t.path).toBe("a.md");
  });

  test("a new tab is inserted right after the active one", () => {
    const strips: TabStrips = {};
    openTab(strips, "p", "a.md");
    openTab(strips, "p", "b.md");
    activateTab(strips, "p", openTab(strips, "p", "a.md").tab.id);
    openTab(strips, "p", "c.md");
    expect(strips.p.tabs.map((x) => x.name)).toEqual(["a.md", "c.md", "b.md"]);
  });

  test("first tab is reactive so load/dirty mutations reach Vue", async () => {
    const strips = reactive<TabStrips>({});
    const { tab: t } = openTab(strips, "p", "a.md");
    expect(isReactive(t)).toBe(true);
    expect(t).toBe(strips.p.tabs[0]);
    let seen: string | undefined;
    const stop = watchEffect(() => { seen = strips.p.tabs[0].status; });
    t.status = "ready";
    await nextTick();
    expect(seen).toBe("ready");
    stop();
  });

  test("strips for projects are independent", () => {
    const strips: TabStrips = {};
    const a = openTab(strips, "a", "a.md").tab;
    openTab(strips, "b", "b.md");
    expect(activeTab(strips, "a")).toBe(a);
    expect(activeTab(strips, "zzz")).toBe(null);
    expect(Object.hasOwn(strips, "zzz")).toBe(false);
  });

  test("removeTab activates right neighbour, then left, then null", () => {
    const strips: TabStrips = {};
    const a = openTab(strips, "p", "a.md").tab;
    const b = openTab(strips, "p", "b.md").tab;
    activateTab(strips, "p", a.id);
    removeTab(strips, "p", a.id);
    expect(strips.p.active).toBe(b.id);
    activateTab(strips, "p", b.id);
    removeTab(strips, "p", b.id);
    expect(strips.p.active).toBe(null);
    expect(strips.p.tabs.length).toBe(0);
    const x = openTab(strips, "p", "x.md").tab;
    const y = openTab(strips, "p", "y.md").tab;
    activateTab(strips, "p", y.id);
    removeTab(strips, "p", y.id);
    expect(strips.p.active).toBe(x.id);
  });

  test("findTab searches all strips", () => {
    const strips: TabStrips = {};
    const a = openTab(strips, "a", "a.md").tab;
    const b = openTab(strips, "b", "b.md").tab;
    expect(findTab(strips, a.id)).toBe(a);
    expect(findTab(strips, b.id)).toBe(b);
    expect(findTab(strips, "nope")).toBe(null);
  });

  test("closeVerdict confirms only when dirty", () => {
    expect(closeVerdict(tab({ dirty: true }))).toBe("confirm");
    expect(closeVerdict(tab({ dirty: false }))).toBe("close");
  });

  test("toggleMode flips a ready .md tab and refuses others", () => {
    const md = tab({ path: "a.md", status: "ready" });
    expect(toggleMode(md)).toBe(true);
    expect(md.mode).toBe("preview");
    expect(toggleMode(md)).toBe(true);
    expect(md.mode).toBe("edit");
    const ts = tab({ path: "a.ts", status: "ready" });
    expect(toggleMode(ts)).toBe(false);
    expect(ts.mode).toBe("edit");
    const loading = tab({ path: "a.md", status: "loading" });
    expect(toggleMode(loading)).toBe(false);
    expect(loading.mode).toBe("edit");
  });

  test("canPreview is markdown and ready only", () => {
    expect(canPreview(tab({ path: "a.md", status: "ready" }))).toBe(true);
    expect(canPreview(tab({ path: "a.md", status: "loading" }))).toBe(false);
    expect(canPreview(tab({ path: "a.ts", status: "ready" }))).toBe(false);
    expect(canPreview(null)).toBe(false);
  });

  test("saveStateOf returns null, dirty or saved", () => {
    expect(saveStateOf(null)).toBe(null);
    expect(saveStateOf(tab({ status: "loading" }))).toBe(null);
    expect(saveStateOf(tab({ status: "ready", dirty: true }))).toBe("dirty");
    expect(saveStateOf(tab({ status: "ready", dirty: false }))).toBe("saved");
  });

  test("languageOf table", () => {
    const table: [string, Lang | null][] = [
      ["a.md", "markdown"], ["a.MDC", "markdown"], ["x/y.markdown", "markdown"],
      ["a.py", "python"], ["a.js", "javascript"], ["a.mjs", "javascript"],
      ["a.cjs", "javascript"], ["a.jsx", "javascript"], ["a.ts", "typescript"],
      ["a.tsx", "typescript"], ["a.mts", "typescript"], ["a.cts", "typescript"],
      ["a.yaml", "yaml"], ["a.yml", "yaml"], ["a.json", "json"],
      ["a.toml", "toml"], ["a.TOML", "toml"],
      ["a.sh", "shell"], ["a.bash", "shell"], ["a.zsh", "shell"],
      ["a.css", "css"], ["a.html", "html"], ["a.vue", "vue"],
      ["a.txt", null], ["noext", null], ["a.xyz", null],
    ];
    for (const [path, lang] of table) expect(languageOf(path)).toBe(lang);
  });
});

describe("syntax colours", () => {
  const samples: Array<[string, string, string]> = [
    ["a.toml", 'name = "lime"\n', '"lime"'],
    ["a.sh", 'echo "hi" # c\n', "# c"],
    ["a.bash", 'echo "hi" # c\n', "# c"],
    ["a.zsh", 'echo "hi" # c\n', "# c"],
    ["a.css", "a { color: red; }\n", "color"],
    ["a.html", "<p>x</p><!-- c -->\n", "<!-- c -->"],
    ["a.vue", '<template><p>x</p></template>\n<script setup lang="ts">\nconst n = 1;\n</script>\n', "const"],
  ];

  test("new file types open with syntax colours", () => {
    for (const [path, src, token] of samples) {
      expect(classOf(src, path, token)).toBeTruthy();
    }
    expect(spans("plain words", "a.txt")).toEqual([]);
    expect(spans("plain words", "a.xyz")).toEqual([]);
  });

  test("markdown fences highlight like their files", () => {
    const rows: Array<[string, string, string, string]> = [
      ["toml", ...samples[0]],
      ["bash", ...samples[1]],
      ["sh", ...samples[1]],
      ["css", ...samples[4]],
      ["html", ...samples[5]],
      ["vue", ...samples[6]],
    ];
    for (const [info, file, src, token] of rows) {
      const md = "```" + info + "\n" + src + "```\n";
      const inMd = classOf(md, "a.md", token);
      expect(inMd).toBeTruthy();
      expect(inMd).toBe(classOf(src, file, token));
    }
  });

  test("legacy-modes is imported per mode, never whole", () => {
    const dir = join(import.meta.dir, "..");
    const files = (readdirSync(dir, { recursive: true }) as string[])
      .map((f) => f.split("\\").join("/"))
      .filter((f) => (f.endsWith(".ts") || f.endsWith(".vue")) && !f.endsWith(".test.ts"));
    const specs: string[] = [];
    for (const f of files) {
      const src = readFileSync(join(dir, f), "utf8");
      for (const m of src.matchAll(/from\s+["'](@codemirror\/legacy-modes[^"']*)["']/g)) specs.push(m[1]);
    }
    for (const s of specs) expect(s).toMatch(/^@codemirror\/legacy-modes\/mode\/(toml|shell)$/);
    const editor = readFileSync(join(dir, "lib/editorTabs.ts"), "utf8");
    expect(editor).toContain("@codemirror/legacy-modes/mode/toml");
    expect(editor).toContain("@codemirror/legacy-modes/mode/shell");
  });

  const PY = 'import os\nfrom os import path  # note\nclass Box:\n    def run(self, x):\n        if x is None or True:\n            return path.sep + str(x, "a", 1)\n';
  const TS = 'import { a } from "b";\n// note\nclass Box { n = 1; }\nconst x: Box = make(a.size, true, null);\nif (x) { x.go(); }\n';

  const categories: Array<[string, string, string]> = [
    ["import", "import", "import"],
    ["control", "if", "if"],
    ["def", "def", "const"],
    ["fn", "str", "make"],
    ["var", "x", "x"],
    ["prop", "sep", "size"],
    ["type", "Box", "Box"],
    ["str", '"a"', '"b"'],
    ["num", "1", "1"],
    ["bool", "None", "true"],
    ["comment", "# note", "// note"],
  ];

  test("python: import, if, def, a function name and a variable get five classes", () => {
    const classes = ["import", "if", "def", "str", "x"].map((k) => classOf(PY, "a.py", k));
    expect(classes.every((c) => c !== undefined)).toBe(true);
    expect(new Set(classes).size).toBe(5);
  });

  test("each code structure gets its own colour in python and ts", () => {
    const colourOf = new Map<string, string>();
    for (const m of calmHighlight.module!.getRules().matchAll(/\.(\S+)\s*\{color: (var\(--syn-[a-z]+\))/g)) {
      colourOf.set(m[1], m[2]);
    }
    const resolved: Array<string | undefined> = [];
    for (const [, py, ts] of categories) {
      const cls = classOf(PY, "a.py", py);
      expect(cls).toBe(classOf(TS, "a.ts", ts));
      resolved.push(cls === undefined ? undefined : colourOf.get(cls));
    }
    for (const c of resolved) {
      expect(c).toBeDefined();
      expect(c).toMatch(/^var\(--syn-[a-z]+\)$/);
    }
    expect(new Set(resolved).size).toBe(11);
    expect(classOf(PY, "a.py", "from")).toBe(classOf(PY, "a.py", "import"));
    expect(classOf(PY, "a.py", "True")).toBe(classOf(PY, "a.py", "None"));
  });

  test("every --syn-* colour derives from palette tokens with a dark value", () => {
    const strip = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");
    const read = (rel: string) => strip(readFileSync(join(import.meta.dir, rel), "utf8"));
    const editorTs = read("editorTabs.ts");

    const used = new Set([...editorTs.matchAll(/var\(--syn-([a-z]+)\)/g)].map((m) => m[1]));
    expect(used.size).toBeGreaterThan(0);
    expect(editorTs).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);

    const vue = read("../components/Editor.vue");
    const block = /\.editor \{([\s\S]*?)\n\}/.exec(vue);
    expect(block).not.toBeNull();
    const declared = new Map<string, string>();
    for (const m of block![1].matchAll(/--syn-([a-z]+):\s*([^;]+);/g)) declared.set(m[1], m[2].trim());
    for (const name of used) expect(declared.has(name)).toBe(true);

    const valueRe = /^(var\(--[a-z-]+\)|color-mix\(in (srgb|oklch), var\(--[a-z-]+\)( \d+%)?, var\(--[a-z-]+\)( \d+%)?\))$/;
    const values = [...declared.values()];
    for (const v of values) expect(v).toMatch(valueRe);
    expect(new Set(values).size).toBe(values.length);

    const props = (css: string, re: RegExp): Set<string> => {
      const m = re.exec(css);
      if (!m) throw new Error(`no block for ${re}`);
      return new Set([...m[1].matchAll(/(--[a-z-]+):/g)].map((x) => x[1]));
    };
    const style = read("../style.css");
    const light = props(style, /^:root \{([\s\S]*?)\n\}/m);
    const darkMedia = props(style, /:root:not\(\[data-theme='light'\]\) \{([\s\S]*?)\n  \}/);
    const darkPinned = props(style, /:root\[data-theme='dark'\] \{([\s\S]*?)\n\}/);
    for (const v of values) {
      for (const m of v.matchAll(/var\((--[a-z-]+)\)/g)) {
        expect(light.has(m[1])).toBe(true);
        expect(darkMedia.has(m[1])).toBe(true);
        expect(darkPinned.has(m[1])).toBe(true);
      }
    }
  });
});

describe("rename follow", () => {
  test("isUnder matches exact, children and not siblings", () => {
    expect(isUnder("src", "src")).toBe(true);
    expect(isUnder("src/a.ts", "src")).toBe(true);
    expect(isUnder("src/x/b.ts", "src")).toBe(true);
    expect(isUnder("src2/a.ts", "src")).toBe(false);
    expect(isUnder("other.ts", "src")).toBe(false);
  });

  test("movedPath re-roots descendants and leaves others", () => {
    expect(movedPath("src/a.ts", "src", "lib")).toBe("lib/a.ts");
    expect(movedPath("src", "src", "lib")).toBe("lib");
    expect(movedPath("src2/a.ts", "src", "lib")).toBe("src2/a.ts");
    expect(movedPath("a.md", "a.md", "b.md")).toBe("b.md");
  });

  test("tabsUnder returns the tabs at/under a path in strip order", () => {
    const strips: TabStrips = {};
    openTab(strips, "p", "src/a.ts");
    openTab(strips, "p", "src/x/b.ts");
    openTab(strips, "p", "src2/c.ts");
    openTab(strips, "p", "other.ts");
    expect(tabsUnder(strips, "p", "src").map((t) => t.path)).toEqual([
      "src/a.ts", "src/x/b.ts",
    ]);
    expect(tabsUnder(strips, "p", "src/a.ts").map((t) => t.path)).toEqual([
      "src/a.ts",
    ]);
    expect(tabsUnder(strips, "zzz", "src")).toEqual([]);
  });

  test("tabsUnder skips a tab whose file lives in another project", () => {
    const strips: TabStrips = {};
    openTab(strips, "p", "src/a.ts");
    const foreign = openTab(strips, "p", "src/a.ts", false, "q").tab;
    expect(tabsUnder(strips, "p", "src").map((t) => t.id)).toEqual([
      tabId("p", "src/a.ts"),
    ]);
    expect(foreign.source).toBe("q");
  });

  test("renameTabs leaves a foreign project's artifact path alone", () => {
    const strips: TabStrips = {};
    const own = openTab(strips, "p", "src/a.ts").tab;
    const foreign = openTab(strips, "p", "src/a.ts", false, "q").tab;
    const foreignId = foreign.id;
    const pairs = renameTabs(strips, "p", "src", "lib");
    expect(pairs).toEqual([[tabId("p", "src/a.ts"), tabId("p", "lib/a.ts")]]);
    expect(own.path).toBe("lib/a.ts");
    expect(foreign.path).toBe("src/a.ts");
    expect(foreign.id).toBe(foreignId);
  });

  test("renameTabs follows a renamed file", () => {
    const strips: TabStrips = {};
    const a = openTab(strips, "p", "a.md").tab;
    const b = openTab(strips, "p", "b.md").tab;
    const q = openTab(strips, "q", "a.md").tab;
    activateTab(strips, "p", a.id);
    const pairs = renameTabs(strips, "p", "a.md", "c.md");
    expect(pairs).toEqual([[tabId("p", "a.md"), tabId("p", "c.md")]]);
    expect(a.path).toBe("c.md");
    expect(a.name).toBe("c.md");
    expect(a.id).toBe(tabId("p", "c.md"));
    expect(strips.p.active).toBe(a.id);
    expect(b.path).toBe("b.md");
    expect(q.path).toBe("a.md");
    expect(q.id).toBe(tabId("q", "a.md"));
  });

  test("renameTabs follows a renamed folder and leaves a non-active tab", () => {
    const strips: TabStrips = {};
    const a = openTab(strips, "p", "src/a.ts").tab;
    const x = openTab(strips, "p", "src/x/b.ts").tab;
    const c = openTab(strips, "p", "src2/c.ts").tab;
    const pairs = renameTabs(strips, "p", "src", "lib");
    expect(a.path).toBe("lib/a.ts");
    expect(x.path).toBe("lib/x/b.ts");
    expect(c.path).toBe("src2/c.ts");
    expect(strips.p.active).toBe(c.id);
    expect(pairs).toHaveLength(2);
  });

  test("renameTabs on a path with no tabs is a no-op", () => {
    const strips: TabStrips = {};
    openTab(strips, "p", "a.md");
    expect(renameTabs(strips, "p", "nope.md", "x.md")).toEqual([]);
  });

  test("renameTabs through reactive() is tracked", async () => {
    const strips = reactive<TabStrips>({});
    openTab(strips, "p", "a.md");
    let seen = "";
    const stop = watchEffect(() => { seen = activeTab(strips, "p")?.path ?? ""; });
    expect(seen).toBe("a.md");
    renameTabs(strips, "p", "a.md", "b.md");
    await nextTick();
    expect(seen).toBe("b.md");
    stop();
  });

  test("a renamed tab saves to its new path", async () => {
    const strips: TabStrips = {};
    const t = openTab(strips, "p", "a.md").tab;
    t.status = "ready";
    renameTabs(strips, "p", "a.md", "b.md");
    const docs = new Map([[t.id, Text.of(["hello"])]]);
    const sent: Array<[string, string, string]> = [];
    const ok = await saveTab(
      t,
      (id) => docs.get(id) ?? null,
      new Map(),
      async (project, path, content) => { sent.push([project, path, content]); return {}; },
    );
    expect(ok).toBe(true);
    expect(sent).toEqual([["p", "b.md", "hello"]]);
  });

  test("only Editor.vue and editorTabs.ts change the tab strips", () => {
    const dir = join(import.meta.dir, "..");
    const files = (readdirSync(dir, { recursive: true }) as string[])
      .map((f) => f.split("\\").join("/"))
      .filter((f) => (f.endsWith(".ts") || f.endsWith(".vue")) && !f.endsWith(".test.ts"));
    const source = (f: string) => readFileSync(join(dir, f), "utf8");
    const stripMutators = files.filter((f) =>
      /\b(openTab|openRunTab|activateTab|removeTab|renameTabs)\b/.test(source(f)),
    );
    expect(stripMutators.sort()).toEqual([
      "components/Editor.vue",
      "components/RunLine.vue",
      "lib/editorTabs.ts",
    ]);
    const savers = files.filter((f) => source(f).includes("saveFileContent"));
    expect(savers.sort()).toEqual(["components/Editor.vue", "lib/api.ts"]);
  });

  test("an artifact opens in the strip on screen, sourced from the Run's project", () => {
    const src = readFileSync(join(import.meta.dir, "../components/Editor.vue"), "utf8");
    // the artifact action opens in props.project, not the Run's own project
    expect(src).toMatch(/openIn\(props\.project, path, r\.project\)/);
    // the fetch reads the tab's source project when it differs
    expect(src).toContain("fetchFileContent(tab.source ?? tab.project, tab.path)");
  });

  test("rename, trash and dirty writers select through the project-guarded tabsUnder", () => {
    const src = readFileSync(join(import.meta.dir, "../components/Editor.vue"), "utf8");
    const body = (name: string) => {
      const at = src.indexOf(`function ${name}(`);
      expect(at).toBeGreaterThan(-1);
      return src.slice(at, src.indexOf("\n}", at));
    };
    // every path-scoped writer goes through tabsUnder, which skips a tab whose
    // file lives in another project, so a foreign artifact is never re-keyed
    // or closed by a rename/trash in the strip's own project.
    expect(body("renamePath")).toContain("renameTabs(strips, props.project");
    for (const name of ["dirtyUnder", "closePath"]) {
      expect(body(name)).toContain("tabsUnder(strips, props.project");
    }
  });
});

describe("editor bindings", () => {
  test("register alongside the shell's other bindings", () => {
    const map = createKeymap();
    expect(() => map.register([
      ...paneBindings({ showAll: noop, toggle: noop }),
      shellEscape(() => false),
      ...settingsBindings(() => {}),
      ...editorBindings({ save: noop, close: noop, preview: noop }),
    ])).not.toThrow();
  });

  test("cmd+S saves once while typing and is prevented", () => {
    const map = createKeymap();
    let saves = 0;
    map.register(editorBindings({ save: () => { saves += 1; }, close: noop, preview: noop }));
    const e = evt({ code: "KeyS", metaKey: true, target: { isContentEditable: true } });
    expect(map.dispatch(e)).toBe(true);
    expect(saves).toBe(1);
    expect(e.preventCount).toBe(1);
  });

  test("alt+W closes and alt+P previews from a textarea", () => {
    const map = createKeymap();
    let closes = 0;
    let previews = 0;
    map.register(editorBindings({
      save: noop, close: () => { closes += 1; }, preview: () => { previews += 1; },
    }));
    const w = evt({ code: "KeyW", altKey: true, target: { tagName: "TEXTAREA" } });
    expect(map.dispatch(w)).toBe(true);
    const p = evt({ code: "KeyP", altKey: true, target: { tagName: "TEXTAREA" } });
    expect(map.dispatch(p)).toBe(true);
    expect(closes).toBe(1);
    expect(previews).toBe(1);
    expect(w.preventCount).toBe(1);
    expect(p.preventCount).toBe(1);
  });

  test("no editor combo uses KeyF", () => {
    for (const b of editorBindings({ save: noop, close: noop, preview: noop })) {
      expect(b.code).not.toBe("KeyF");
      expect(b.key).not.toBe("f");
    }
  });
});

describe("CodeMirror state", () => {
  test("markdown state binds Mod-f / Mod-g and is writable", () => {
    const state = createTabState("x", "a.md", noop);
    const bindings = state.facet(keymap).flat();
    expect(bindings.find((b) => b.key === "Mod-f")?.run).toBe(openSearchPanel);
    expect(bindings.find((b) => b.key === "Mod-g")?.run).toBe(findNext);
    expect(state.readOnly).toBe(false);
  });

  test("two tabs get two distinct states", () => {
    const a = createTabState("a", "a.md", noop);
    const b = createTabState("b", "b.md", noop);
    expect(a).not.toBe(b);
    expect(a.doc.toString()).toBe("a");
    expect(b.doc.toString()).toBe("b");
  });

  test("undo is per state", () => {
    const state = createTabState("abc", "a.ts", noop);
    const next = state.update({ changes: { from: 3, insert: "d" } }).state;
    expect(next.doc.toString()).toBe("abcd");
    let out: EditorState | undefined;
    undo({ state: next, dispatch: (tr) => { out = tr.state; } });
    expect(out?.doc.toString()).toBe("abc");
  });
});

describe("saveTab", () => {
  const docOf = (docs: Map<string, Text>) => (id: string) => docs.get(id) ?? null;

  test("success clears dirty and stores the saved doc", async () => {
    const t = tab();
    const docs = new Map([[t.id, Text.of(["abc"])]]);
    const saved = new Map<string, Text>();
    let sent = "";
    const ok = await saveTab(t, docOf(docs), saved, async (_p, _f, content) => {
      sent = content;
      return {};
    });
    expect(ok).toBe(true);
    expect(sent).toBe("abc");
    expect(t.dirty).toBe(false);
    expect(saved.get(t.id)?.toString()).toBe("abc");
    expect(t.saving).toBe(false);
  });

  test("a rejected save stays dirty with the server message", async () => {
    const t = tab();
    const docs = new Map([[t.id, Text.of(["abc"])]]);
    const ok = await saveTab(t, docOf(docs), new Map(), async () => {
      throw new FileApiError("file not found", 404, "not_found");
    });
    expect(ok).toBe(false);
    expect(t.dirty).toBe(true);
    expect(t.error).toContain("file not found");
    expect(t.saving).toBe(false);
  });

  test("editing while the PUT is pending keeps the tab dirty", async () => {
    const t = tab();
    const docs = new Map([[t.id, Text.of(["abc"])]]);
    const saved = new Map<string, Text>();
    let release: () => void = noop;
    const gate = new Promise<void>((r) => { release = r; });
    const p = saveTab(t, docOf(docs), saved, async () => { await gate; return {}; });
    docs.set(t.id, Text.of(["abcd"]));
    release();
    expect(await p).toBe(true);
    expect(t.dirty).toBe(true);
    expect(saved.get(t.id)?.toString()).toBe("abc");
  });

  test("a second call while saving does not call put", async () => {
    const t = tab();
    const docs = new Map([[t.id, Text.of(["abc"])]]);
    const saved = new Map<string, Text>();
    let calls = 0;
    let release: () => void = noop;
    const gate = new Promise<void>((r) => { release = r; });
    const p = saveTab(t, docOf(docs), saved, async () => { calls += 1; await gate; return {}; });
    const second = await saveTab(t, docOf(docs), saved, async () => { calls += 1; return {}; });
    expect(second).toBe(false);
    expect(calls).toBe(1);
    release();
    await p;
  });
});

describe("read-only tabs", () => {
  test("openTab marks a read-only tab and defaults to writable", () => {
    const strips: TabStrips = {};
    const { tab: ro } = openTab(strips, "p", "focus.md", true);
    expect(ro.readOnly).toBe(true);
    const { tab: rw } = openTab(strips, "p", "a.md");
    expect(rw.readOnly).toBeFalsy();
  });

  test("saveTab never writes a read-only tab", async () => {
    const t = tab({ readOnly: true });
    const docs = new Map([[t.id, Text.of(["abc"])]]);
    let puts = 0;
    const ok = await saveTab(
      t, (id) => docs.get(id) ?? null, new Map(),
      async () => { puts += 1; return {}; },
    );
    expect(ok).toBe(false);
    expect(puts).toBe(0);
  });

  test("saveStateOf is null for a read-only tab", () => {
    expect(saveStateOf(tab({ readOnly: true, dirty: false }))).toBe(null);
  });

  test("createTabState honours the read-only flag", () => {
    expect(createTabState("x", "focus.md", noop, true).readOnly).toBe(true);
    expect(createTabState("x", "focus.md", noop).readOnly).toBe(false);
  });
});

describe("run tabs", () => {
  test("openRunTab creates an active, ready, read-only tab with its run", () => {
    const strips: TabStrips = {};
    const run = { project: "proj-a", runId: "r1" };
    const { tab: t, created } = openRunTab(strips, "cur", run, "my-run");
    expect(created).toBe(true);
    expect(strips.cur.active).toBe(t.id);
    expect(activeTab(strips, "cur")).toBe(t);
    expect(t.status).toBe("ready");
    expect(t.readOnly).toBe(true);
    expect(t.run).toEqual(run);
    expect(t.path).toBe(runTabPath(run));
    expect(t.name).toBe("my-run");
    expect(t.id).not.toBe(tabId("cur", runTabPath(run)));
  });

  test("a new Run tab is inserted right after the active file tab", () => {
    const strips: TabStrips = {};
    openTab(strips, "cur", "a.md");
    openTab(strips, "cur", "b.md");
    activateTab(strips, "cur", openTab(strips, "cur", "a.md").tab.id);
    openRunTab(strips, "cur", { project: "p", runId: "r" }, "r");
    expect(strips.cur.tabs.map((t) => t.name)).toEqual(["a.md", "r", "b.md"]);
  });

  test("re-opening a Run reuses and activates it; a different Run is new", () => {
    const strips: TabStrips = {};
    const run = { project: "p", runId: "r" };
    const first = openRunTab(strips, "cur", run, "r").tab;
    openTab(strips, "cur", "a.md");
    const again = openRunTab(strips, "cur", run, "r");
    expect(again.created).toBe(false);
    expect(again.tab.id).toBe(first.id);
    expect(strips.cur.active).toBe(first.id);
    expect(strips.cur.tabs.length).toBe(2);
    openRunTab(strips, "cur", { project: "p", runId: "r2" }, "r2");
    expect(strips.cur.tabs.length).toBe(3);
  });

  test("a Run from another project lands in the strip's own project", () => {
    const strips: TabStrips = {};
    const run = { project: "other", runId: "r" };
    openRunTab(strips, "cur", run, "r");
    expect(strips.cur.tabs.length).toBe(1);
    expect(strips.cur.tabs[0].run).toEqual(run);
    expect(strips.other).toBeUndefined();
  });

  test("an artifact from another project opens in the strip on screen, reading its source", () => {
    const strips: TabStrips = {};
    const { tab: t, created } = openTab(strips, "cur", "plan.md", false, "other");
    expect(created).toBe(true);
    expect(strips.cur.tabs).toEqual([t]);
    expect(strips.other).toBeUndefined();
    expect(t.project).toBe("cur");
    expect(t.source).toBe("other");
    expect(t.id).toBe(fileTabId("cur", "plan.md", "other"));
    // re-opening the same source reuses the tab
    const again = openTab(strips, "cur", "plan.md", false, "other");
    expect(again.created).toBe(false);
    expect(again.tab.id).toBe(t.id);
    // the same path from the strip's own project is a distinct tab
    const own = openTab(strips, "cur", "plan.md");
    expect(own.created).toBe(true);
    expect(own.tab.id).toBe(tabId("cur", "plan.md"));
    expect(own.tab.source).toBeUndefined();
  });

  test("a sourced tab saves to its source project", async () => {
    const strips: TabStrips = {};
    const t = openTab(strips, "cur", "plan.md", false, "other").tab;
    t.status = "ready";
    const docs = new Map([[t.id, Text.of(["hello"])]]);
    const sent: Array<[string, string, string]> = [];
    const ok = await saveTab(
      t,
      (id) => docs.get(id) ?? null,
      new Map(),
      async (project, path, content) => { sent.push([project, path, content]); return {}; },
    );
    expect(ok).toBe(true);
    expect(sent).toEqual([["other", "plan.md", "hello"]]);
  });

  test("openTab on a Run path makes a separate file tab", () => {
    const strips: TabStrips = {};
    const run = { project: "p", runId: "r" };
    openRunTab(strips, "cur", run, "r");
    const { tab: f, created } = openTab(strips, "cur", runTabPath(run));
    expect(created).toBe(true);
    expect(f.run).toBeUndefined();
    expect(strips.cur.tabs.length).toBe(2);
  });

  test("tabsUnder and renameTabs never touch a Run tab", () => {
    const strips: TabStrips = {};
    const rt = openRunTab(strips, "cur", { project: "proj-a", runId: "r" }, "r").tab;
    const id = rt.id;
    expect(tabsUnder(strips, "cur", "run:proj-a")).toEqual([]);
    expect(renameTabs(strips, "cur", "run:proj-a", "x")).toEqual([]);
    expect(rt.path).toBe("run:proj-a/r");
    expect(rt.name).toBe("r");
    expect(rt.id).toBe(id);
  });

  test("a Run tab is read-only and clean", async () => {
    const strips: TabStrips = {};
    const t = openRunTab(strips, "cur", { project: "p", runId: "r" }, "r").tab;
    expect(saveStateOf(t)).toBe(null);
    expect(closeVerdict(t)).toBe("close");
    expect(canPreview(t)).toBe(false);
    let puts = 0;
    const ok = await saveTab(
      t, () => Text.of(["x"]), new Map(),
      async () => { puts += 1; return {}; },
    );
    expect(ok).toBe(false);
    expect(puts).toBe(0);
  });

  test("runTabName strips the ticket path to its stem, else the run id", () => {
    expect(runTabName("/x/.lime/tickets/228-runs-sheet.md", "r1")).toBe("228-runs-sheet");
    expect(runTabName(null, "r1")).toBe("r1");
    expect(runTabName(undefined, "r1")).toBe("r1");
  });

  test("a new Run tab gets a fresh dock; a reused tab keeps its own", () => {
    const strips: TabStrips = {};
    const run = { project: "p", runId: "r" };
    const first = openRunTab(strips, "cur", run, "r").tab;
    expect(first.dock).toEqual({ runOpen: false, active: "phase" });
    expect(newRunDock()).toEqual({ runOpen: false, active: "phase" });

    first.dock!.runOpen = true;
    first.dock!.active = "run";
    const again = openRunTab(strips, "cur", run, "r");
    expect(again.created).toBe(false);
    expect(again.tab.dock).toEqual({ runOpen: true, active: "run" });
  });

  test("toggleRunDock opens and activates, then closes on the second call", () => {
    const d = newRunDock();
    toggleRunDock(d);
    expect(d).toEqual({ runOpen: true, active: "run" });
    toggleRunDock(d);
    expect(d).toEqual({ runOpen: false, active: "phase" });

    // With the phase tab active, [Run] is open but not shown: toggle shows it.
    const e: RunDock = { runOpen: true, active: "phase" };
    toggleRunDock(e);
    expect(e).toEqual({ runOpen: true, active: "run" });
  });

  test("escapeDock closes [Run] when shown, then the phase, then nothing", () => {
    const d: RunDock = { runOpen: true, active: "run" };
    expect(escapeDock(d, true)).toBe("run");
    expect(d).toEqual({ runOpen: false, active: "phase" });
    expect(escapeDock(d, true)).toBe("phase");
    expect(escapeDock(d, false)).toBeNull();

    // [Run] open but the phase tab shown: Esc drops the phase first.
    const e: RunDock = { runOpen: true, active: "phase" };
    expect(escapeDock(e, true)).toBe("phase");
    expect(e.runOpen).toBe(true);
  });

  test("dockActive truth table", () => {
    const cases: Array<[RunDock, boolean, "run" | "phase" | null]> = [
      [{ runOpen: false, active: "phase" }, true, "phase"],
      [{ runOpen: false, active: "phase" }, false, null],
      [{ runOpen: false, active: "run" }, true, "phase"],
      [{ runOpen: false, active: "run" }, false, null],
      [{ runOpen: true, active: "phase" }, true, "phase"],
      [{ runOpen: true, active: "phase" }, false, "run"],
      [{ runOpen: true, active: "run" }, true, "run"],
      [{ runOpen: true, active: "run" }, false, "run"],
    ];
    for (const [d, phaseOpen, want] of cases) {
      expect(dockActive(d, phaseOpen)).toBe(want);
    }
  });

  test("closeRunDock and showPhaseDock move only their own field", () => {
    const d: RunDock = { runOpen: true, active: "run" };
    showPhaseDock(d);
    expect(d).toEqual({ runOpen: true, active: "phase" });
    closeRunDock(d);
    expect(d).toEqual({ runOpen: false, active: "phase" });
  });
});

describe("confirm buttons", () => {
  const read = (rel: string) => readFileSync(join(import.meta.dir, rel), "utf8");
  const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");

  // The declarations of the rule whose selector exactly equals `selector` at a
  // line start (leading whitespace allowed); `.btn.primary` must not match
  // `.btn.primary:hover`. Split declarations only on the first `:`.
  function rule(css: string, selector: string): Record<string, string> {
    const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const re = new RegExp(`^[ \\t]*${esc}[ \\t]*\\{([^}]*)\\}`, "m");
    const m = re.exec(css);
    if (!m) throw new Error(`no rule for ${selector}`);
    const out: Record<string, string> = {};
    for (const decl of m[1].split(";")) {
      const d = decl.trim();
      if (!d) continue;
      const i = d.indexOf(":");
      if (i < 0) continue;
      out[d.slice(0, i).trim()] = d.slice(i + 1).trim();
    }
    return out;
  }

  function themes() {
    const css = stripComments(read("../style.css"));
    const light = rule(css, ":root");
    return {
      light,
      darkMedia: { ...light, ...rule(css, ":root:not([data-theme='light'])") },
      darkPinned: { ...light, ...rule(css, ":root[data-theme='dark']") },
    };
  }

  // Follow one level of var(): var(--x) -> the theme's value for --x.
  function resolve(value: string, theme: Record<string, string>): string {
    let v = value.trim();
    for (let i = 0; i < 2; i++) {
      const m = /^var\((--[\w-]+)\)$/.exec(v);
      if (!m) break;
      v = (theme[m[1]] ?? "").trim();
    }
    return v;
  }

  function luminance(hex: string): number {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
    if (!m) throw new Error(`not a hex colour: ${hex}`);
    const n = parseInt(m[1], 16);
    const lin = [(n >> 16) & 255, (n >> 8) & 255, n & 255]
      .map((v) => v / 255)
      .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
  }

  function contrast(a: string, b: string): number {
    const la = luminance(a);
    const lb = luminance(b);
    const hi = Math.max(la, lb);
    const lo = Math.min(la, lb);
    return (hi + 0.05) / (lo + 0.05);
  }

  const SIZE_KEYS = [
    "height", "min-height", "padding", "line-height", "border",
    "margin", "font-size", "transform",
  ];
  const FILES = ["../components/Editor.vue", "../components/FileTree.vue"];
  const T = themes();

  test("the primary label is dark text on lime in light and dark", () => {
    for (const file of FILES) {
      const css = stripComments(read(file));
      const primary = rule(css, ".btn.primary");
      for (const theme of Object.values(T)) {
        const lime = resolve(primary.background, theme);
        expect(lime).toBe(theme["--lime"]);
        const fg = resolve(primary.color, theme);
        expect(contrast(fg, lime)).toBeGreaterThanOrEqual(4.5);
        expect(luminance(fg)).toBeLessThan(luminance(lime));
      }
    }
  });

  test("both confirms keep one button box and an in-place focus ring", () => {
    const boxes = FILES.map((f) => rule(stripComments(read(f)), ".btn"));
    expect(boxes[0]).toEqual(boxes[1]);
    for (const btn of boxes) {
      expect(btn.height).toBe("30px");
      expect(btn.display).toBe("inline-flex");
      expect(btn["align-items"]).toBe("center");
    }
    for (const file of FILES) {
      const css = stripComments(read(file));
      for (const sel of [".btn.primary", ".btn.ghost"]) {
        const r = rule(css, sel);
        for (const k of SIZE_KEYS) expect(r[k]).toBeUndefined();
      }
      const focus = rule(css, ".btn:focus-visible");
      for (const k of SIZE_KEYS) expect(focus[k]).toBeUndefined();
      const width = parseFloat(/[\d.]+px/.exec(focus.outline)![0]);
      const offset = parseFloat(focus["outline-offset"]);
      expect(offset).toBeLessThanOrEqual(-width);
    }
  });
});

describe("find bar", () => {
  let dom: JSDOM;
  const installed: string[] = [];
  const views: Array<{ view: EditorView; parent: HTMLElement }> = [];
  const GLOBAL_KEYS = [
    "window", "document", "MutationObserver", "requestAnimationFrame",
    "cancelAnimationFrame", "getComputedStyle", "HTMLElement", "Node", "KeyboardEvent",
  ];
  const BOUND_FNS = ["requestAnimationFrame", "cancelAnimationFrame", "getComputedStyle"];

  beforeAll(() => {
    dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true });
    const g = globalThis as unknown as Record<string, unknown>;
    const w = dom.window as unknown as Record<string, unknown>;
    for (const key of GLOBAL_KEYS) {
      if (g[key] !== undefined) continue;
      const raw = w[key];
      const value = BOUND_FNS.includes(key) && typeof raw === "function"
        ? (raw as (...a: unknown[]) => unknown).bind(dom.window)
        : raw;
      Object.defineProperty(globalThis, key, { value, configurable: true });
      installed.push(key);
    }
  });

  afterAll(() => {
    const g = globalThis as unknown as Record<string, unknown>;
    for (const key of installed) delete g[key];
    dom.window.close();
  });

  afterEach(() => {
    while (views.length) {
      const { view, parent } = views.pop()!;
      view.destroy();
      parent.remove();
    }
  });

  const mount = (text: string): EditorView => {
    const parent = document.createElement("div");
    document.body.appendChild(parent);
    const view = new EditorView({ state: createTabState(text, "a.ts", noop), parent });
    views.push({ view, parent });
    view.focus();
    return view;
  };

  const bar = (): HTMLElement => document.querySelector(".cm-panels-top .cm-find") as HTMLElement;
  const input = (): HTMLInputElement => document.querySelector(".cm-find-input") as HTMLInputElement;
  const count = (): string => document.querySelector(".cm-find-count")?.textContent ?? "";
  const buttonList = (): HTMLElement[] => Array.from(document.querySelectorAll(".cm-find-btn"));
  const button = (name: string): HTMLButtonElement =>
    document.querySelector(`.cm-find-btn[name="${name}"]`) as HTMLButtonElement;

  const type = (_view: EditorView, s: string): void => {
    const el = input();
    el.value = s;
    el.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
  };

  const press = (_view: EditorView, key: string, shiftKey = false): void => {
    input().dispatchEvent(new dom.window.KeyboardEvent("keydown", {
      key, shiftKey, bubbles: true, cancelable: true,
    }));
  };

  test("⌘F opens one find row with no replace or option controls", () => {
    const view = mount("foo");
    openSearchPanel(view);
    expect(document.querySelectorAll(".cm-panels-top .cm-find").length).toBe(1);
    expect(document.querySelector(".cm-search")).toBeNull();
    const b = bar();
    expect(b.querySelectorAll("input").length).toBe(1);
    expect(b.querySelectorAll("input[type=checkbox]").length).toBe(0);
    expect(b.querySelector("[name=replace]")).toBeNull();
    expect(b.querySelector("select")).toBeNull();
    expect(buttonList().map((e) => e.getAttribute("name"))).toEqual(["prev", "next", "close"]);
    expect(count()).toBe("0/0");
    expect(document.activeElement).toBe(input());
  });

  test("Enter and ↓ go forward, ⇧Enter and ↑ go back, wrapping at the ends", () => {
    const view = mount("foo x foo x foo");
    openSearchPanel(view);
    type(view, "foo");
    expect(count()).toBe("0/3");

    press(view, "Enter");
    expect(view.state.selection.main.from).toBe(0);
    expect(view.state.selection.main.to).toBe(3);
    expect(count()).toBe("1/3");

    button("next").click();
    expect(view.state.selection.main.from).toBe(6);
    expect(count()).toBe("2/3");

    button("next").click();
    expect(view.state.selection.main.from).toBe(12);
    expect(count()).toBe("3/3");

    press(view, "Enter");
    expect(view.state.selection.main.from).toBe(0);
    expect(count()).toBe("1/3");

    press(view, "Enter", true);
    expect(view.state.selection.main.from).toBe(12);
    expect(count()).toBe("3/3");

    button("prev").click();
    expect(view.state.selection.main.from).toBe(6);
    expect(count()).toBe("2/3");
  });

  test("Esc closes the bar and returns focus to the editor", () => {
    const view = mount("foo");
    openSearchPanel(view);
    press(view, "Escape");
    expect(searchPanelOpen(view.state)).toBe(false);
    expect(document.querySelector(".cm-find")).toBeNull();
    expect(document.activeElement).toBe(view.contentDOM);

    openSearchPanel(view);
    button("close").click();
    expect(searchPanelOpen(view.state)).toBe(false);
    expect(document.querySelector(".cm-find")).toBeNull();
    expect(document.activeElement).toBe(view.contentDOM);
  });

  test("an empty or matchless query shows 0/0 and the arrows move nothing", () => {
    const view = mount("foo bar");
    openSearchPanel(view);
    view.dispatch({ selection: { anchor: 1 } });
    const at = () => view.state.selection.main;
    const still = () => at().from === 1 && at().to === 1;

    button("next").click();
    button("prev").click();
    press(view, "Enter");
    press(view, "Enter", true);
    expect(still()).toBe(true);
    expect(input().value).toBe("");
    expect(count()).toBe("0/0");

    type(view, "zzz");
    button("next").click();
    button("prev").click();
    press(view, "Enter");
    press(view, "Enter", true);
    expect(count()).toBe("0/0");
    expect(still()).toBe(true);
  });

  test("⌘F over a selected word seeds the field", () => {
    const view = mount("foo bar foo");
    view.dispatch({ selection: { anchor: 0, head: 3 } });
    openSearchPanel(view);
    expect(input().value).toBe("foo");
    expect(count()).toBe("1/2");
  });

  test("only editorTabs.ts imports @codemirror/search", () => {
    const dir = join(import.meta.dir, "..");
    const files = (readdirSync(dir, { recursive: true }) as string[])
      .map((f) => f.split("\\").join("/"))
      .filter((f) => (f.endsWith(".ts") || f.endsWith(".vue")) && !f.endsWith(".test.ts"));
    const importers = files.filter((f) =>
      /from\s+["']@codemirror\/search["']/.test(readFileSync(join(dir, f), "utf8")),
    );
    expect(importers.sort()).toEqual(["lib/editorTabs.ts"]);
  });
});
