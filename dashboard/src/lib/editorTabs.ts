/**
 * Editor tab model, shell bindings and per-tab CodeMirror state.
 *
 * Pure TypeScript, no Vue import: the tab functions mutate a plain strips
 * object, which the component hands in as `reactive()`, so every mutation is
 * tracked. Every tab owns one `EditorState`, so undo history and selection live
 * in that state and survive tab and project switches.
 */
import { EditorState, type Extension, type Text } from "@codemirror/state";
import {
  EditorView, keymap, lineNumbers, highlightActiveLine,
  highlightActiveLineGutter, drawSelection, dropCursor, highlightSpecialChars,
  runScopeHandlers, type Panel, type ViewUpdate,
} from "@codemirror/view";
import {
  HighlightStyle, StreamLanguage, syntaxHighlighting, bracketMatching, indentOnInput,
  indentUnit, LanguageSupport, type Language,
} from "@codemirror/language";
import { tags as t, styleTags } from "@lezer/highlight";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { search, searchKeymap, SearchQuery, getSearchQuery, setSearchQuery, findNext, findPrevious, closeSearchPanel } from "@codemirror/search";
import { markdown, markdownLanguage } from "@codemirror/lang-markdown";
import { javascript, javascriptLanguage, typescriptLanguage } from "@codemirror/lang-javascript";
import { python, pythonLanguage } from "@codemirror/lang-python";
import { yaml, yamlLanguage } from "@codemirror/lang-yaml";
import { json, jsonLanguage } from "@codemirror/lang-json";
import { css, cssLanguage } from "@codemirror/lang-css";
import { html, htmlLanguage } from "@codemirror/lang-html";
import { vue, vueLanguage } from "@codemirror/lang-vue";
import { toml } from "@codemirror/legacy-modes/mode/toml";
import { shell } from "@codemirror/legacy-modes/mode/shell";
import { isMarkdownPath } from "./markdown";
import { glyph } from "./fileIcons";
import type { Binding } from "./keymap";

// ── A. Tab model ────────────────────────────────────────────────────────────

export type TabMode = "edit" | "preview";
export type TabStatus = "loading" | "ready" | "error";
export type SaveState = null | "dirty" | "saved";

/** The project a Run ran in, which may differ from the strip's project. */
export interface RunRef { project: string; runId: string }

/** The dock's [Run] tab state: whether it is open, and which tab is shown. */
export interface RunDock { runOpen: boolean; active: "run" | "phase" }

export interface FileTab {
  id: string; project: string; path: string; name: string;
  mode: TabMode; status: TabStatus; dirty: boolean; saving: boolean;
  error: string | null; readOnly?: boolean;
  /** The project to read/write the file from, when it differs from the strip's
   *  project — a Run artifact opened from another project's Run tab. */
  source?: string;
  /** Present means this is a Run detail tab, never a file tab. */
  run?: RunRef;
  /** Run tabs only: the dock's [Run] tab state, kept per tab. */
  dock?: RunDock;
  /** Run tabs only: the phase this tab last selected, kept per tab so
   *  switching through another Run never discards it (PLACEMENT row 81). */
  phase?: string | null;
}

/** A fresh dock: [Run] closed, the phase tab shown. */
export function newRunDock(): RunDock {
  return { runOpen: false, active: "phase" };
}

/** The dock tab actually shown: [Run] when it is open and either active or the
 *  phase tab is closed, else the phase tab when it is open, else nothing. */
export function dockActive(
  d: RunDock, phaseOpen: boolean,
): "run" | "phase" | null {
  if (d.runOpen && (d.active === "run" || !phaseOpen)) return "run";
  return phaseOpen ? "phase" : null;
}

/** Close [Run] and show the phase tab. */
export function closeRunDock(d: RunDock): void {
  d.runOpen = false;
  d.active = "phase";
}

/** Show the phase tab, leaving [Run]'s open state alone. */
export function showPhaseDock(d: RunDock): void {
  d.active = "phase";
}

/** Open and show [Run], or close it when it is already the shown tab. */
export function toggleRunDock(d: RunDock): void {
  if (d.runOpen && d.active === "run") {
    closeRunDock(d);
    return;
  }
  d.runOpen = true;
  d.active = "run";
}

/**
 * One Esc step in the dock: close [Run] when it is the shown tab, else report
 * the phase tab so the caller drops the phase from the link, else nothing.
 */
export function escapeDock(
  d: RunDock, phaseOpen: boolean,
): "run" | "phase" | null {
  if (dockActive(d, phaseOpen) === "run") {
    closeRunDock(d);
    return "run";
  }
  return phaseOpen ? "phase" : null;
}
export interface TabStrip { tabs: FileTab[]; active: string | null }
/** Keyed by project — tabs are per project (quiz Q5). */
export type TabStrips = Record<string, TabStrip>;

export function tabId(project: string, path: string): string {
  return `${project}\u0000${path}`;
}
/** A file tab's id: the strip project, the source project when it differs, and
 *  the path. Run tab ids carry `run`, so they never collide with a file tab. */
export function fileTabId(project: string, path: string, source?: string): string {
  return source && source !== project
    ? `${project}\u0000${source}\u0000${path}`
    : tabId(project, path);
}
/** The identity and display string of a Run tab — never a file path. */
export function runTabPath(run: RunRef): string {
  return `run:${run.project}/${run.runId}`;
}
/** A Run tab id carries `run`, so it can never equal a file tab id. */
export function runTabId(project: string, run: RunRef): string {
  return `${project}\u0000run\u0000${run.project}\u0000${run.runId}`;
}
const basename = (path: string): string => path.split("/").pop() || path;

/** The ticket's basename without `.md`, else the run id when there is none. */
export function runTabName(ticket: string | null | undefined, runId: string): string {
  if (!ticket) return runId;
  return basename(ticket).replace(/\.md$/, "");
}

/** Insert `tab` right after the active one (or the end) and activate it. */
function insertTab(strip: TabStrip, tab: FileTab): void {
  const at = strip.active ? strip.tabs.findIndex((e) => e.id === strip.active) : -1;
  if (at >= 0) strip.tabs.splice(at + 1, 0, tab);
  else strip.tabs.push(tab);
  strip.active = tab.id;
}

/**
 * Reuse or create the tab for `path`. Creates the project's strip when it is
 * missing, inserts a new tab right after the active one (or the end) and
 * activates it. Returns the element read back from `strip.tabs`, so a caller
 * holding a reactive store gets the reactive proxy.
 */
export function openTab(
  strips: TabStrips, project: string, path: string, readOnly = false, source?: string,
): { tab: FileTab; created: boolean } {
  // Assign first, then read back, so a reactive store hands us the proxy:
  // mutating/returning the raw literals would bypass Vue's tracking.
  if (!strips[project]) strips[project] = { tabs: [], active: null };
  const strip = strips[project];
  const src = source && source !== project ? source : undefined;
  const existing = strip.tabs.find(
    (tab) => tab.path === path && !tab.run && tab.source === src,
  );
  if (existing) {
    strip.active = existing.id;
    return { tab: strip.tabs[strip.tabs.indexOf(existing)], created: false };
  }
  const id = fileTabId(project, path, src);
  const tab: FileTab = {
    id, project, path, name: basename(path), mode: "edit",
    status: "loading", dirty: false, saving: false, error: null, readOnly,
    ...(src ? { source: src } : {}),
  };
  insertTab(strip, tab);
  return { tab: strip.tabs[strip.tabs.findIndex((e) => e.id === id)], created: true };
}

/**
 * Reuse or create the tab for a Run. Mirrors `openTab`: creates the project's
 * strip when missing, reuses a tab whose `run` matches, inserts a new read-only
 * tab right after the active one (or the end) and activates it. The Run's own
 * project may differ from `project`; the tab still lands in `strips[project]`.
 */
export function openRunTab(
  strips: TabStrips, project: string, run: RunRef, name: string,
): { tab: FileTab; created: boolean } {
  if (!strips[project]) strips[project] = { tabs: [], active: null };
  const strip = strips[project];
  const existing = strip.tabs.find(
    (tab) => tab.run && tab.run.project === run.project && tab.run.runId === run.runId,
  );
  if (existing) {
    strip.active = existing.id;
    return { tab: strip.tabs[strip.tabs.indexOf(existing)], created: false };
  }
  const id = runTabId(project, run);
  const tab: FileTab = {
    id, project, path: runTabPath(run), name, mode: "edit",
    status: "ready", dirty: false, saving: false, error: null,
    readOnly: true, run: { ...run }, dock: newRunDock(), phase: null,
  };
  insertTab(strip, tab);
  return { tab: strip.tabs[strip.tabs.findIndex((e) => e.id === id)], created: true };
}

/** Read-only: never creates a strip. */
export function activeTab(strips: TabStrips, project: string | null): FileTab | null {
  if (!project) return null;
  const strip = strips[project];
  if (!strip || !strip.active) return null;
  return strip.tabs.find((tab) => tab.id === strip.active) ?? null;
}

export function findTab(strips: TabStrips, id: string): FileTab | null {
  for (const strip of Object.values(strips)) {
    const tab = strip.tabs.find((e) => e.id === id);
    if (tab) return tab;
  }
  return null;
}

export function activateTab(strips: TabStrips, project: string, id: string): void {
  const strip = strips[project];
  if (strip && strip.tabs.some((tab) => tab.id === id)) strip.active = id;
}

/** When the removed tab was active, the right neighbour becomes active, else the left, else null. */
export function removeTab(strips: TabStrips, project: string, id: string): void {
  const strip = strips[project];
  if (!strip) return;
  const at = strip.tabs.findIndex((tab) => tab.id === id);
  if (at < 0) return;
  const wasActive = strip.active === id;
  strip.tabs.splice(at, 1);
  if (wasActive) {
    const next = strip.tabs[at] ?? strip.tabs[at - 1] ?? null;
    strip.active = next ? next.id : null;
  }
}

/** True when `path` is `root` itself or a descendant at/under it. */
export function isUnder(path: string, root: string): boolean {
  return path === root || path.startsWith(root + "/");
}

/** The path a `from`-rename gives `path`: `to` plus the trailing segment.
 *  Paths outside `from` are returned unchanged. */
export function movedPath(path: string, from: string, to: string): string {
  return isUnder(path, from) ? to + path.slice(from.length) : path;
}

/** The project a tab's file actually lives in: the Run's project for an
 *  artifact opened from another project's Run tab, else the strip's project. */
export function fileProject(tab: FileTab): string {
  return tab.source ?? tab.project;
}

/** The project strip's tabs at/under `path` whose file lives in `project`, in
 *  strip order. A tab whose file belongs to another project (a Run artifact
 *  opened from a foreign Run tab) shares the strip but not the tree, so a
 *  rename or trash in `project` must never touch it. Read through `strip.tabs`
 *  so a reactive store hands back its proxies. */
export function tabsUnder(
  strips: TabStrips, project: string, path: string,
): FileTab[] {
  const strip = strips[project];
  if (!strip) return [];
  return strip.tabs.filter(
    (tab) => !tab.run && fileProject(tab) === project && isUnder(tab.path, path),
  );
}

/** Follow a file/folder rename in a project's tabs, in place. Each affected
 *  tab's `path`, `name` and `id` are re-keyed and the strip's `active` follows,
 *  since the id embeds the path. Returns the `[oldId, newId]` pairs. */
export function renameTabs(
  strips: TabStrips, project: string, from: string, to: string,
): Array<[string, string]> {
  const strip = strips[project];
  const pairs: Array<[string, string]> = [];
  if (!strip) return pairs;
  for (const tab of tabsUnder(strips, project, from)) {
    const oldId = tab.id;
    const newPath = movedPath(tab.path, from, to);
    tab.path = newPath;
    tab.name = basename(newPath);
    tab.id = fileTabId(project, newPath, tab.source);
    if (strip.active === oldId) strip.active = tab.id;
    pairs.push([oldId, tab.id]);
  }
  return pairs;
}

export function closeVerdict(tab: FileTab): "close" | "confirm" {
  return tab.dirty ? "confirm" : "close";
}
export function canPreview(tab: FileTab | null): boolean {
  return !!tab && isMarkdownPath(tab.path) && tab.status === "ready";
}
/** Flip edit↔preview when preview is available; otherwise leave `mode` alone. */
export function toggleMode(tab: FileTab): boolean {
  if (!canPreview(tab)) return false;
  tab.mode = tab.mode === "edit" ? "preview" : "edit";
  return true;
}
export function saveStateOf(tab: FileTab | null): SaveState {
  if (!tab || tab.readOnly || tab.status !== "ready") return null;
  return tab.dirty ? "dirty" : "saved";
}

export type Lang = "markdown" | "python" | "javascript" | "typescript" | "yaml" | "json" | "toml" | "shell" | "css" | "html" | "vue";
export function languageOf(path: string): Lang | null {
  switch (path.split(".").pop()?.toLowerCase() ?? "") {
    case "md": case "markdown": case "mdc": return "markdown";
    case "py": return "python";
    case "js": case "mjs": case "cjs": case "jsx": return "javascript";
    case "ts": case "tsx": case "mts": case "cts": return "typescript";
    case "yaml": case "yml": return "yaml";
    case "json": return "json";
    case "toml": return "toml";
    case "sh": case "bash": case "zsh": return "shell";
    case "css": return "css";
    case "html": return "html";
    case "vue": return "vue";
    default: return null;
  }
}
export const LANG_LABEL: Record<Lang | "plain", string> = {
  markdown: "Markdown", python: "Python", javascript: "JavaScript",
  typescript: "TypeScript", yaml: "YAML", json: "JSON", plain: "Plain text",
  toml: "TOML", shell: "Shell", css: "CSS", html: "HTML", vue: "Vue",
};

// ── B. Shell bindings ───────────────────────────────────────────────────────

/**
 * ⌘S / ⌥W / ⌥P. Each sets `whileTyping: true` and returns nothing, which counts
 * as handled: the shell prevents and stops the event, so the browser's Save
 * Page dialog never opens and no `∑`/`π` is typed.
 *
 * ⌘F is deliberately absent. It belongs to CodeMirror's editor keymap
 * (`searchKeymap`), so it only fires inside the editor.
 */
export function editorBindings(a: {
  save(): void; close(): void; preview(): void;
}): Binding[] {
  return [
    { id: "editor: save", code: "KeyS", mod: true, whileTyping: true, run: () => a.save() },
    { id: "editor: close tab", code: "KeyW", alt: true, whileTyping: true, run: () => a.close() },
    { id: "editor: preview", code: "KeyP", alt: true, whileTyping: true, run: () => a.preview() },
  ];
}

// ── C. CodeMirror per tab ───────────────────────────────────────────────────

// Colours come from CSS variables, so one theme serves light and dark.
const calmTheme = EditorView.theme({
  "&": { height: "100%", fontSize: "13px", backgroundColor: "transparent", color: "var(--text)" },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { fontFamily: "var(--mono)", lineHeight: "1.7", overflow: "auto" },
  ".cm-content": { caretColor: "var(--caret)", padding: "14px 0 30vh" },
  ".cm-line": { padding: "0 24px 0 6px" },
  ".cm-gutters": { backgroundColor: "transparent", border: "none", color: "var(--faint)" },
  ".cm-lineNumbers .cm-gutterElement": { padding: "0 10px 0 20px", minWidth: "48px", fontSize: "11.5px" },
  ".cm-activeLine": { backgroundColor: "var(--cm-active)" },
  ".cm-activeLineGutter": { backgroundColor: "transparent", color: "var(--muted)" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--caret)", borderLeftWidth: "2px" },
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection":
    { backgroundColor: "var(--cm-sel)" },
  ".cm-matchingBracket, &.cm-focused .cm-matchingBracket": { backgroundColor: "var(--cm-match)", outline: "none" },
  ".cm-panels": { backgroundColor: "var(--panel)", color: "var(--text)" },
  ".cm-panels.cm-panels-top": { borderBottom: "1px solid var(--line)" },
  ".cm-find": { display: "flex", alignItems: "center", gap: "4px", padding: "6px 10px", fontFamily: "var(--sans)" },
  ".cm-find-field": {
    display: "flex", alignItems: "center", gap: "6px", width: "280px", height: "28px",
    padding: "0 8px", borderRadius: "7px", backgroundColor: "var(--panel-2)", border: "1px solid var(--line)",
  },
  ".cm-find-input": {
    flex: "1", minWidth: "0", border: "none", outline: "none", backgroundColor: "transparent",
    font: "inherit", fontSize: "12.5px", color: "var(--text)",
  },
  ".cm-find-count": {
    flex: "none", color: "var(--faint)", fontFamily: "var(--mono)", fontSize: "11px",
    fontVariantNumeric: "tabular-nums",
  },
  ".cm-find-btn": {
    width: "24px", height: "24px", display: "inline-flex", alignItems: "center", justifyContent: "center",
    borderRadius: "6px", color: "var(--muted)", fontSize: "14px",
  },
  ".cm-find-btn:hover:not(:disabled)": { backgroundColor: "var(--hover)", color: "var(--text)" },
  ".cm-find-btn:disabled": { opacity: ".4", cursor: "default" },
});

// Created once and reused by both langExt and codeLanguages.
const tomlLanguage = StreamLanguage.define(toml);
const shellLanguage = StreamLanguage.define(shell);

// lezer-python tags `from` as a definition keyword; inside an import it is an import keyword.
const pyLanguage = pythonLanguage.configure({ props: [styleTags({ "ImportStatement/from": t.moduleKeyword })] });

export const calmHighlight = HighlightStyle.define([
  { tag: t.moduleKeyword, color: "var(--syn-import)" },
  { tag: [t.keyword, t.controlKeyword, t.operatorKeyword, t.modifier], color: "var(--syn-control)" },
  { tag: t.definitionKeyword, color: "var(--syn-def)" },
  { tag: [t.string, t.special(t.string), t.regexp, t.attributeValue], color: "var(--syn-str)" },
  { tag: [t.comment, t.lineComment, t.blockComment, t.docComment], color: "var(--syn-comment)", fontStyle: "italic" },
  { tag: [t.number, t.unit, t.color], color: "var(--syn-num)" },
  { tag: [t.bool, t.null, t.atom], color: "var(--syn-bool)" },
  { tag: [t.function(t.variableName), t.function(t.propertyName), t.definition(t.function(t.variableName)), t.standard(t.variableName)], color: "var(--syn-fn)" },
  { tag: t.variableName, color: "var(--syn-var)" },
  { tag: [t.className, t.typeName, t.namespace, t.definition(t.className), t.tagName], color: "var(--syn-type)" },
  { tag: [t.propertyName, t.attributeName], color: "var(--syn-prop)" },
  { tag: [t.self, t.special(t.variableName)], color: "var(--syn-def)", fontStyle: "italic" },
  { tag: [t.operator, t.punctuation, t.separator, t.bracket], color: "var(--syn-op)" },
  { tag: [t.meta, t.annotation, t.processingInstruction, t.documentMeta], color: "var(--syn-comment)" },
  { tag: t.heading, color: "var(--syn-heading)", fontWeight: "650" },
  { tag: t.heading1, color: "var(--syn-heading)", fontWeight: "680", fontSize: "1.3em", letterSpacing: "-.01em" },
  { tag: t.heading2, color: "var(--syn-heading)", fontWeight: "650", fontSize: "1.12em" },
  { tag: t.strong, fontWeight: "650" },
  { tag: t.emphasis, fontStyle: "italic" },
  { tag: [t.link, t.url], color: "var(--syn-fn)", textDecoration: "underline" },
  { tag: t.monospace, color: "var(--syn-str)", fontFamily: "var(--mono)", fontSize: ".88em" },
  { tag: t.quote, color: "var(--syn-quote)" },
  { tag: t.invalid, color: "var(--syn-invalid)" },
]);

const codeLanguages = (info: string): Language | null => {
  const k = (info || "").toLowerCase();
  if (k === "python" || k === "py") return pyLanguage;
  if (k === "js" || k === "javascript" || k === "mjs") return javascriptLanguage;
  if (k === "ts" || k === "typescript") return typescriptLanguage;
  if (k === "yaml" || k === "yml") return yamlLanguage;
  if (k === "json") return jsonLanguage;
  if (k === "toml") return tomlLanguage;
  if (k === "bash" || k === "sh" || k === "shell" || k === "zsh") return shellLanguage;
  if (k === "css") return cssLanguage;
  if (k === "html") return htmlLanguage;
  if (k === "vue") return vueLanguage;
  return null;
};

function langExt(lang: Lang | null): Extension {
  switch (lang) {
    case "python": return [new LanguageSupport(pyLanguage, python().support), indentUnit.of("    ")];
    case "markdown": return markdown({ base: markdownLanguage, codeLanguages });
    case "javascript": return javascript();
    case "typescript": return javascript({ typescript: true });
    case "yaml": return yaml();
    case "json": return json();
    case "toml": return tomlLanguage;
    case "shell": return shellLanguage;
    case "css": return css();
    case "html": return html();
    case "vue": return vue();
    default: return [];
  }
}

const base: Extension[] = [
  highlightSpecialChars(), history(), drawSelection(), dropCursor(), indentOnInput(),
  bracketMatching(), highlightActiveLine(), syntaxHighlighting(calmHighlight), calmTheme,
];

/**
 * Where the selection sits in the query's matches. `at` is 1-based, or 0 when
 * the selection is on no match; `total` is 0 for an invalid or empty query.
 * Typing does not move the selection (CodeMirror's stock behaviour), so the
 * count reads `0/N` until Enter or ↓.
 */
export function findCount(state: EditorState): { at: number; total: number } {
  const query = getSearchQuery(state);
  if (!query.valid) return { at: 0, total: 0 };
  const sel = state.selection.main;
  let at = 0, total = 0;
  const cursor = query.getCursor(state);
  for (let m = cursor.next(); !m.done; m = cursor.next()) {
    total += 1;
    if (m.value.from === sel.from && m.value.to === sel.to) at = total;
  }
  return { at, total };
}

/** The find bar: one row, no replace, no checkboxes. Keys use DOM properties. */
function findBar(view: EditorView): Panel {
  const doc = view.dom.ownerDocument;
  const dom = doc.createElement("div");
  dom.className = "cm-find";
  dom.setAttribute("role", "search");

  const field = doc.createElement("div");
  field.className = "cm-find-field";

  const input = doc.createElement("input");
  input.className = "cm-find-input";
  input.setAttribute("main-field", "true");
  input.placeholder = "Find";
  input.setAttribute("aria-label", "Find in file");
  input.spellcheck = false;
  input.autocomplete = "off";
  input.value = getSearchQuery(view.state).search;

  const counter = doc.createElement("span");
  counter.className = "cm-find-count";
  counter.setAttribute("aria-live", "polite");

  field.append(input, counter);

  const mkBtn = (name: string, label: string): HTMLButtonElement => {
    const b = doc.createElement("button");
    b.className = "cm-find-btn";
    b.type = "button";
    b.name = name;
    b.title = label;
    b.setAttribute("aria-label", label);
    return b;
  };
  const prev = mkBtn("prev", "Previous match (⇧Enter)");
  prev.textContent = "↑";
  const next = mkBtn("next", "Next match (Enter)");
  next.textContent = "↓";
  const close = mkBtn("close", "Close (Esc)");
  close.innerHTML = glyph("x", 13);

  dom.append(field, prev, next, close);

  const step = (dir: number): void => {
    if (findCount(view.state).total === 0) return;
    (dir > 0 ? findNext : findPrevious)(view);
  };

  const render = (): void => {
    const { at, total } = findCount(view.state);
    counter.textContent = `${at}/${total}`;
    prev.disabled = total === 0;
    next.disabled = total === 0;
  };

  input.oninput = () => {
    view.dispatch({ effects: setSearchQuery.of(new SearchQuery({ search: input.value })) });
  };

  // DOM properties, not addEventListener: keymap.ts is the only keydown listener.
  dom.onkeydown = (e) => {
    if (runScopeHandlers(view, e, "search-panel")) { e.preventDefault(); return; }
    if (e.key === "Enter" && e.target === input) { e.preventDefault(); step(e.shiftKey ? -1 : 1); }
  };

  const still = (e: Event): void => e.preventDefault();
  prev.onmousedown = still;
  next.onmousedown = still;
  close.onmousedown = still;
  prev.onclick = () => step(-1);
  next.onclick = () => step(1);
  close.onclick = () => closeSearchPanel(view);

  render();

  return {
    dom,
    top: true,
    mount() { input.focus(); input.select(); },
    update(u: ViewUpdate) {
      let queryChanged = false;
      for (const tr of u.transactions) {
        for (const e of tr.effects) {
          if (e.is(setSearchQuery)) {
            queryChanged = true;
            if (e.value.search !== input.value) input.value = e.value.search;
          }
        }
      }
      if (queryChanged || u.docChanged || u.selectionSet) render();
    },
  };
}

export function tabExtensions(path: string, onDoc: (doc: Text) => void, readOnly = false): Extension {
  return [
    base,
    search({ top: true, createPanel: findBar }),
    keymap.of([...searchKeymap, indentWithTab, ...defaultKeymap, ...historyKeymap]),
    isMarkdownPath(path)
      ? [EditorView.lineWrapping, EditorView.editorAttributes.of({ class: "cm-prose" })]
      : [lineNumbers(), highlightActiveLineGutter()],
    langExt(languageOf(path)),
    ...(readOnly ? [EditorState.readOnly.of(true)] : []),
    EditorView.updateListener.of((u) => { if (u.docChanged) onDoc(u.state.doc); }),
  ];
}

export function createTabState(text: string, path: string, onDoc: (doc: Text) => void, readOnly = false): EditorState {
  return EditorState.create({ doc: text, extensions: tabExtensions(path, onDoc, readOnly) });
}

/**
 * Persist one tab's document. Returns false when the tab is not ready, a save
 * is already in flight, or its state is gone. On success it stores the sent doc
 * as the saved baseline and recomputes `dirty` from the current doc, so edits
 * made while the PUT was pending keep the tab dirty. A throw leaves `dirty`
 * untouched and records the server's message in `error`.
 */
export async function saveTab(
  tab: FileTab, docOf: (id: string) => Text | null, saved: Map<string, Text>,
  put: (project: string, path: string, content: string) => Promise<unknown>,
): Promise<boolean> {
  if (tab.readOnly) return false;
  if (tab.status !== "ready" || tab.saving) return false;
  const doc = docOf(tab.id);
  if (!doc) return false;
  tab.saving = true;
  tab.error = null;
  try {
    await put(tab.source ?? tab.project, tab.path, doc.toString());
    saved.set(tab.id, doc);
    const now = docOf(tab.id);
    tab.dirty = !!now && !now.eq(doc);
    return true;
  } catch (e) {
    tab.error = "Save failed: " + (e instanceof Error ? e.message : String(e));
    return false;
  } finally {
    tab.saving = false;
  }
}
