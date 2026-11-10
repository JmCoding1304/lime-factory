<script setup lang="ts">
// The middle-pane editor: a per-project strip holding file tabs and read-only
// Run tabs, each with its own CodeMirror state, a markdown Preview/Edit toggle
// and a close-confirm. Ported from prototype B (proto-workspace/b: editor.js,
// styles.css:251–326) and wired to the pure logic in lib/editorTabs.ts. The tab
// model, save and CodeMirror state live there; this file owns the view, the
// reactive strips object and the shell bindings. A Run tab renders RunView
// inside the same `.run-host`.
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { EditorView } from '@codemirror/view'
import type { EditorState, Text } from '@codemirror/state'
import {
  activeTab,
  activateTab,
  canPreview,
  closeVerdict,
  createTabState,
  editorBindings,
  findTab,
  LANG_LABEL,
  languageOf,
  openRunTab,
  openTab,
  removeTab,
  renameTabs,
  saveStateOf,
  saveTab,
  tabsUnder,
  toggleMode,
} from '../lib/editorTabs'
import type { FileTab, RunRef, SaveState, TabStrips } from '../lib/editorTabs'
import { fetchFileContent, saveFileContent } from '../lib/api'
import { tabPhase, useRoute } from '../lib/router'
import { useKeys } from '../lib/keymap'
import { renderMarkdown } from '../lib/markdown'
import { fileIconSvg, glyph } from '../lib/fileIcons'
import RunView from './RunView.vue'

/** The tab-strip glyph for a Run: the activity mark at the same 14px stroke. */
const RUN_ICON = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>`

const props = defineProps<{ project: string | null }>()
const emit = defineEmits<{
  state: [s: { path: string | null; save: SaveState }]
  runs: [refs: RunRef[]]
}>()

// ── Reactive tab model. Never put an EditorState/EditorView inside it ────────
const strips = reactive<TabStrips>({})
const strip = computed(() => (props.project ? strips[props.project] : undefined))
const active = computed(() => activeTab(strips, props.project))

// Every open Run tab across project strips — the live feed's subscribed set.
const openRuns = computed(() =>
  Object.values(strips).flatMap((s) => s.tabs.flatMap((t) => (t.run ? [t.run] : []))),
)
watch(openRuns, (refs) => emit('runs', refs), { immediate: true })
const route = useRoute()

// ── Non-reactive CodeMirror state ────────────────────────────────────────────
let view: EditorView | null = null
let current: string | null = null // the tab id whose state currently sits in the view
const states = new Map<string, EditorState>()
const saved = new Map<string, Text>()
const scrolls = new Map<string, number>()

const cmHost = ref<HTMLElement | null>(null)
const runView = ref<InstanceType<typeof RunView> | null>(null)

/** The live doc: read it from the view when it is the mounted tab, else the stash. */
function docOf(id: string): Text | null {
  if (id === current && view) return view.state.doc
  return states.get(id)?.doc ?? null
}

// ── Open / load ──────────────────────────────────────────────────────────────
function openIn(project: string, path: string, source?: string) {
  const { tab, created } = openTab(strips, project, path, false, source)
  if (created || tab.status === 'error') void load(tab)
}

function open(path: string) {
  if (!props.project) return
  openIn(props.project, path)
}

/** A Run's artifact opens in the strip on screen, reading from the Run's own
 *  project, which may differ from that strip's project (including All). */
function openArtifact(path: string) {
  const r = active.value?.run
  if (!r || !props.project) return
  openIn(props.project, path, r.project)
}

/** Open a read-only Run detail tab in the current project's strip. */
function openRun(run: RunRef, name: string) {
  if (!props.project) return
  openRunTab(strips, props.project, run, name)
}

/** The phase the active Run tab shows: the link's when it names that Run, else
 *  the phase the tab last selected (PLACEMENT row 81). */
const runPhase = computed(() => {
  const t = active.value
  return t && t.run ? tabPhase(route.value, t.run, t.phase ?? null) : null
})

/** Keep the active Run tab's own selection, so switching through another Run
 *  does not discard it. */
function onRunPhase(id: string | null) {
  const t = active.value
  if (t && t.run) t.phase = id
}

/** Open a non-project document (focus.md) read-only; re-opening refreshes its text. */
function openReadOnly(path: string, text: string) {
  if (!props.project) return
  const { tab } = openTab(strips, props.project, path, true)
  if (current === tab.id) current = null
  const st = createTabState(text, path, () => {}, true)
  states.set(tab.id, st)
  saved.set(tab.id, st.doc)
  tab.status = 'ready'
  tab.error = null
  tab.dirty = false
  void nextTick(sync)
}

async function load(tab: FileTab) {
  tab.status = 'loading'
  tab.error = null
  try {
    const fc = await fetchFileContent(tab.source ?? tab.project, tab.path)
    if (!findTab(strips, tab.id)) return // the tab closed while loading
    const st = createTabState(fc.content, tab.path, (doc) => {
      tab.dirty = !doc.eq(saved.get(tab.id) ?? doc)
    })
    states.set(tab.id, st)
    saved.set(tab.id, st.doc)
    tab.status = 'ready'
  } catch (e) {
    if (!findTab(strips, tab.id)) return
    tab.status = 'error'
    tab.error = (e as Error).message
  }
}

// ── Mount the view and follow the active tab ─────────────────────────────────
function sync() {
  const t = active.value
  const wanted = t && !t.run && t.status === 'ready' && t.mode === 'edit' ? t.id : null
  if (wanted === current) return
  if (view && current) {
    states.set(current, view.state)
    scrolls.set(current, view.scrollDOM.scrollTop)
  }
  current = null
  if (!view || !wanted) return
  const st = states.get(wanted)
  if (!st) return
  view.setState(st)
  current = wanted
  view.scrollDOM.scrollTop = scrolls.get(wanted) ?? 0
  view.requestMeasure()
}

watch(
  () => [props.project, active.value?.id, active.value?.mode, active.value?.status],
  () => void nextTick(sync),
)

onMounted(() => {
  view = new EditorView({ parent: cmHost.value! })
  sync()
})
onBeforeUnmount(() => {
  view?.destroy()
})

// ── Save / close ─────────────────────────────────────────────────────────────
function save(tab: FileTab): Promise<boolean> {
  return saveTab(tab, docOf, saved, saveFileContent)
}

function drop(tab: FileTab) {
  if (current === tab.id) current = null // close the mounted tab without stashing
  states.delete(tab.id)
  saved.delete(tab.id)
  scrolls.delete(tab.id)
  removeTab(strips, tab.project, tab.id)
}

/** Follow a file/folder rename: re-key the per-tab stashes onto the new ids so
 *  undo history, saved-doc and scroll survive, and keep `current`/`confirmId`
 *  pointing at the same tab. */
function renamePath(from: string, to: string) {
  if (!props.project) return
  for (const [oldId, newId] of renameTabs(strips, props.project, from, to)) {
    const st = states.get(oldId)
    if (st) {
      states.delete(oldId)
      states.set(newId, st)
    }
    const sv = saved.get(oldId)
    if (sv) {
      saved.delete(oldId)
      saved.set(newId, sv)
    }
    const sc = scrolls.get(oldId)
    if (sc !== undefined) {
      scrolls.delete(oldId)
      scrolls.set(newId, sc)
    }
    if (current === oldId) current = newId
    if (confirmId.value === oldId) confirmId.value = newId
  }
}

/** Names of the project's dirty tabs at/under `path`, for a Trash warning. */
function dirtyUnder(path: string): string[] {
  if (!props.project) return []
  return tabsUnder(strips, props.project, path)
    .filter((tab) => tab.dirty)
    .map((tab) => tab.name)
}

/** Close every tab at/under `path`. No save prompt: the Trash confirm already
 *  warned that their edits are lost. */
function closePath(path: string) {
  if (!props.project) return
  for (const tab of tabsUnder(strips, props.project, path)) {
    if (confirmId.value === tab.id) confirmId.value = null
    drop(tab)
  }
}

const confirmId = ref<string | null>(null)
const confirmTab = computed(() =>
  confirmId.value ? findTab(strips, confirmId.value) : null,
)
const confirmSaveEl = ref<HTMLButtonElement | null>(null)

function requestClose(tab: FileTab) {
  if (closeVerdict(tab) === 'confirm') confirmId.value = tab.id
  else drop(tab)
}

async function confirmSave() {
  const t = confirmTab.value
  confirmId.value = null
  if (t && (await save(t)) && !t.dirty) drop(t)
}

function confirmDiscard() {
  const t = confirmTab.value
  confirmId.value = null
  if (t) drop(t)
}

function confirmCancel() {
  confirmId.value = null
}

// Focus the Save button once the dialog is up, so Enter saves.
watch(confirmId, (id) => {
  if (id) void nextTick(() => confirmSaveEl.value?.focus())
})

/** The shell's Escape: close the confirm if it is up, else decline. */
function escape(): boolean {
  if (confirmId.value) {
    confirmId.value = null
    return true
  }
  // App overlays close first; the dock answers next (NOTES §10).
  return runView.value?.escape() ?? false
}

// ── Preview toggle, keys and links ───────────────────────────────────────────
function togglePreview() {
  const t = active.value
  if (t && toggleMode(t) && t.mode === 'edit') void nextTick(() => view?.focus())
}

function setMode(mode: 'edit' | 'preview') {
  const t = active.value
  if (!t || !canPreview(t)) return
  t.mode = mode
  if (mode === 'edit') void nextTick(() => view?.focus())
}

useKeys(
  editorBindings({
    save: () => {
      if (active.value) void save(active.value)
    },
    close: () => {
      if (active.value) requestClose(active.value)
    },
    preview: togglePreview,
  }),
)

const previewHtml = computed(() => {
  const t = active.value
  return t && t.mode === 'preview' && canPreview(t)
    ? renderMarkdown(docOf(t.id)?.toString() ?? '')
    : ''
})

/** Only https(s) links leave the page; every other link does nothing. */
function onPreviewClick(e: MouseEvent) {
  const a = (e.target as HTMLElement | null)?.closest('a')
  if (!a) return
  e.preventDefault()
  const href = a.getAttribute('href') ?? ''
  if (/^https?:\/\//i.test(href)) window.open(href, '_blank', 'noopener,noreferrer')
}

function onAux(e: MouseEvent, t: FileTab) {
  if (e.button === 1) requestClose(t)
}

// ── Top-bar save state ───────────────────────────────────────────────────────
watch(
  () => [active.value?.path ?? null, saveStateOf(active.value)] as const,
  ([path, saveState]) => emit('state', { path, save: saveState }),
  { immediate: true },
)

defineExpose({ open, openRun, openReadOnly, escape, renamePath, dirtyUnder, closePath })
</script>

<template>
  <div class="editor">
    <div class="tabbar">
      <TransitionGroup tag="div" class="tabs" name="tab" :key="project ?? ''">
        <div
          v-for="t in strip?.tabs ?? []"
          :key="t.id"
          class="tab"
          :class="{ active: t.id === active?.id, dirty: t.dirty }"
          :title="t.run ? `Run ${t.run.runId} · ${t.run.project}` : t.path"
          @click="activateTab(strips, t.project, t.id)"
          @mousedown.middle.prevent
          @auxclick="onAux($event, t)"
        >
          <span class="tab-icon" v-html="t.run ? RUN_ICON : fileIconSvg(t.path, 14)" />
          <span class="tab-name">{{ t.name }}</span>
          <span class="tab-end">
            <span class="tab-dot" />
            <button
              class="tab-x"
              title="Close  ⌥W"
              @click.stop="requestClose(t)"
              v-html="glyph('x', 12, 2)"
            />
          </span>
        </div>
      </TransitionGroup>
    </div>

    <div v-if="active && !active.run" class="docbar">
      <span class="doc-path" :title="active.path">{{ active.path }}</span>
      <div class="doc-tools">
        <span v-if="active.status === 'ready' && active.error" class="doc-error">
          {{ active.error }}
        </span>
        <span v-if="active.readOnly" class="doc-note">Read-only</span>
        <div
          v-if="canPreview(active)"
          class="mini-seg"
          :class="{ 'at-edit': active.mode === 'edit' }"
          title="Toggle  ⌥P"
        >
          <span class="mini-thumb" />
          <button :class="{ on: active.mode === 'preview' }" @click="setMode('preview')">
            Preview
          </button>
          <button :class="{ on: active.mode === 'edit' }" @click="setMode('edit')">Edit</button>
        </div>
        <span v-else class="doc-note">{{ LANG_LABEL[languageOf(active.path) ?? 'plain'] }}</span>
      </div>
    </div>

    <div class="doc">
      <div
        v-show="active?.mode === 'preview'"
        class="preview"
        @click="onPreviewClick"
      >
        <article class="prose" v-html="previewHtml" />
      </div>
      <div
        v-show="!!active && !active.run && active.status === 'ready' && active.mode === 'edit'"
        ref="cmHost"
        class="cm-host"
      />
      <div v-if="active?.run" class="run-host">
        <RunView
          v-if="active.dock"
          ref="runView"
          :key="active.id"
          :project="active.run.project"
          :run-id="active.run.runId"
          :phase="runPhase"
          :dock="active.dock"
          @open="openArtifact"
          @phase="onRunPhase"
        />
      </div>
      <div v-if="active?.status === 'loading'" class="doc-status">Loading…</div>
      <div v-if="active?.status === 'error'" class="doc-error-center">{{ active.error }}</div>
    </div>

    <Teleport to="body">
      <div v-if="confirmId" class="confirm-layer">
        <div class="confirm-scrim" @click="confirmCancel" />
        <div class="confirm" role="alertdialog">
          <div class="confirm-t">Save changes to {{ confirmTab?.name }}?</div>
          <div class="confirm-s">Closing without saving drops your edits.</div>
          <div class="pop-actions">
            <button class="btn ghost" @click="confirmCancel">Cancel</button>
            <button class="btn ghost" @click="confirmDiscard">Don't save</button>
            <button ref="confirmSaveEl" class="btn primary" @click="confirmSave">Save</button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
/* The `.ws` root maps --panel/--panel-2/--muted/--line/--line-soft/--hover/
   --press/--ease for descendants; the syntax and CodeMirror tokens are local.
   Every --syn-* derives only from palette tokens style.css re-declares for dark, so each has a light and a dark value. */
.editor {
  --syn-import: var(--bad);
  --syn-control: var(--run);
  --syn-def: color-mix(in oklch, var(--run) 50%, var(--bad));
  --syn-fn: color-mix(in oklch, var(--ok) 30%, var(--run));
  --syn-var: color-mix(in srgb, var(--text) 80%, var(--lime-ink));
  --syn-prop: color-mix(in srgb, var(--text) 50%, var(--run));
  --syn-type: color-mix(in oklch, var(--ok) 65%, var(--run));
  --syn-str: var(--ok);
  --syn-num: var(--warn);
  --syn-bool: color-mix(in oklch, var(--warn) 50%, var(--bad));
  --syn-comment: var(--faint);
  --syn-op: var(--dim);
  --syn-heading: color-mix(in srgb, var(--text) 70%, var(--run));
  --syn-quote: color-mix(in srgb, var(--text) 40%, var(--faint));
  --syn-invalid: color-mix(in oklch, var(--bad) 70%, var(--text));
  --cm-active: color-mix(in srgb, var(--text) 3%, transparent);
  --cm-sel: color-mix(in srgb, var(--lime) 28%, transparent);
  --cm-match: color-mix(in srgb, var(--lime) 32%, transparent);
  --caret: var(--text);
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

/* ── tabbar ─────────────────────────────────────────────────────────────── */
.tabbar {
  flex: none;
  height: 46px;
  display: flex;
  align-items: center;
  padding: 0 8px;
  overflow-x: auto;
  scrollbar-width: none;
}
.tabbar::-webkit-scrollbar {
  display: none;
}
.tabs {
  display: flex;
  align-items: center;
}
.tab {
  position: relative;
  flex: none;
  display: flex;
  align-items: center;
  gap: 7px;
  height: 30px;
  padding: 0 6px 0 10px;
  margin-right: 4px;
  border-radius: 8px;
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  cursor: default;
  user-select: none;
  max-width: 260px;
  transition: background 0.12s, color 0.12s;
}
.tab:hover {
  background: var(--hover);
  color: var(--text);
}
.tab.active {
  background: var(--panel-2);
  color: var(--text);
  box-shadow: inset 0 0 0 1px var(--line-soft);
}
.tab-icon {
  display: inline-flex;
  opacity: 0.85;
}
.tab-name {
  font-size: 13px;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.tab.active .tab-name {
  font-weight: 540;
}
.tab-end {
  position: relative;
  width: 18px;
  height: 18px;
  flex: none;
}
.tab-dot {
  position: absolute;
  inset: 5.5px;
  border-radius: 50%;
  background: var(--text);
  opacity: 0;
  transform: scale(0.4);
  transition: opacity 0.12s, transform 0.14s var(--ease);
}
.tab.dirty .tab-dot {
  opacity: 0.75;
  transform: none;
}
.tab-x {
  position: absolute;
  inset: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 5px;
  color: var(--muted);
  opacity: 0;
  transition: opacity 0.1s, background 0.1s;
}
.tab:hover .tab-x {
  opacity: 1;
}
.tab.dirty:hover .tab-dot {
  opacity: 0;
}
.tab-x:hover {
  background: var(--press);
  color: var(--text);
}
.tab-leave-active {
  transition: max-width 0.16s var(--ease), opacity 0.12s;
  overflow: hidden;
}
.tab-leave-to {
  max-width: 0;
  opacity: 0;
}

/* ── docbar ─────────────────────────────────────────────────────────────── */
.docbar {
  flex: none;
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 12px 0 18px;
  border-bottom: 1px solid var(--line-soft);
}
.doc-path {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--muted);
}
.doc-tools {
  flex: none;
  display: flex;
  align-items: center;
  gap: 12px;
}
.doc-note {
  font-size: 12px;
  color: var(--faint);
}
.doc-error {
  font-size: 12px;
  color: var(--bad);
}
.mini-seg {
  position: relative;
  display: inline-flex;
  padding: 2px;
  border-radius: 8px;
  background: var(--hover);
}
.mini-seg button {
  position: relative;
  z-index: 1;
  width: 62px;
  height: 22px;
  font-size: 12px;
  color: var(--muted);
  border-radius: 6px;
  transition: color 0.12s;
}
.mini-seg button.on {
  color: var(--text);
  font-weight: 540;
}
.mini-thumb {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 62px;
  height: 22px;
  border-radius: 6px;
  background: var(--panel);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08), 0 0 0 0.5px var(--line);
  transition: transform 0.16s var(--ease);
}
.mini-seg.at-edit .mini-thumb {
  transform: translateX(62px);
}

/* ── document ───────────────────────────────────────────────────────────── */
.doc {
  position: relative;
  flex: 1;
  min-height: 0;
}
.preview,
.cm-host {
  position: absolute;
  inset: 0;
}
.run-host {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.preview {
  overflow: auto;
  outline: none;
  scrollbar-width: thin;
  scrollbar-color: var(--line) transparent;
}
.cm-host :deep(.cm-editor) {
  height: 100%;
}
.cm-host :deep(.cm-scroller) {
  scrollbar-width: thin;
  scrollbar-color: var(--line) transparent;
}
:deep(.cm-editor.cm-prose) {
  font-size: 15px;
}
:deep(.cm-editor.cm-prose .cm-scroller) {
  font-family: var(--sans);
  line-height: 1.7;
}
:deep(.cm-editor.cm-prose .cm-content) {
  max-width: 72ch;
  margin: 0 auto;
  padding: 36px 0 30vh;
}
:deep(.cm-editor.cm-prose .cm-line) {
  padding: 0 40px;
}
:deep(.cm-editor.cm-prose:not(.cm-focused) .cm-activeLine) {
  background: transparent;
}
.doc-status {
  padding: 18px 22px;
  font-size: 12.5px;
  color: var(--faint);
}
.doc-error-center {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  text-align: center;
  font-size: 13px;
  color: var(--bad);
}

/* the markdown preview reading column */
.prose {
  max-width: 72ch;
  margin: 0 auto;
  padding: 36px 40px 30vh;
  font-size: 15.5px;
  line-height: 1.7;
  color: var(--text);
}
:deep(.prose > :first-child) {
  margin-top: 0;
}
:deep(.prose h1) {
  font-size: 1.75em;
  line-height: 1.2;
  letter-spacing: -0.02em;
  font-weight: 680;
  margin: 0 0 0.9em;
}
:deep(.prose h2) {
  font-size: 1.22em;
  letter-spacing: -0.01em;
  font-weight: 640;
  margin: 2em 0 0.6em;
}
:deep(.prose h3) {
  font-size: 1.04em;
  font-weight: 640;
  margin: 1.7em 0 0.5em;
}
:deep(.prose p) {
  margin: 0 0 1em;
}
:deep(.prose ul),
:deep(.prose ol) {
  padding-left: 1.4em;
  margin: 0 0 1em;
}
:deep(.prose li) {
  margin: 0.28em 0;
}
:deep(.prose li::marker) {
  color: var(--faint);
}
:deep(.prose a) {
  color: var(--lime-ink);
  text-decoration: underline;
  text-underline-offset: 2px;
}
:deep(.prose code) {
  font-size: 0.84em;
  font-family: var(--mono);
  background: var(--panel-2);
  border: 1px solid var(--line-soft);
  padding: 0.1em 0.38em;
  border-radius: 5px;
  overflow-wrap: anywhere;
}
:deep(.prose pre) {
  background: var(--panel-2);
  border: 1px solid var(--line-soft);
  border-radius: 10px;
  padding: 14px 16px;
  overflow: auto;
  font-size: 13px;
  line-height: 1.6;
}
:deep(.prose pre code) {
  background: none;
  border: 0;
  padding: 0;
  font-size: 1em;
}
:deep(.prose blockquote) {
  margin: 0 0 1em;
  padding: 0.1em 0 0.1em 1em;
  border-left: 3px solid var(--line);
  color: var(--muted);
}
:deep(.prose hr) {
  border: 0;
  border-top: 1px solid var(--line);
  margin: 2em 0;
}
:deep(.prose table) {
  border-collapse: collapse;
  width: 100%;
  font-size: 0.88em;
  margin: 0 0 1.2em;
  display: block;
  overflow-x: auto;
}
:deep(.prose th),
:deep(.prose td) {
  text-align: left;
  padding: 7px 10px;
  border-bottom: 1px solid var(--line-soft);
  vertical-align: top;
}
:deep(.prose strong) {
  font-weight: 640;
}

/* ── confirm dialog (teleported under <body>, outside .ws) ────────────────── */
.confirm-layer {
  position: fixed;
  inset: 0;
  z-index: 80;
  display: flex;
  align-items: center;
  justify-content: center;
}
.confirm-scrim {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.32);
  backdrop-filter: blur(2px);
}
.confirm {
  position: relative;
  width: 380px;
  padding: 20px;
  background: var(--surface);
  color: var(--text);
  border-radius: 16px;
  box-shadow: var(--shadow);
}
.confirm-t {
  font-weight: 640;
  font-size: 14.5px;
  margin-bottom: 4px;
  overflow-wrap: anywhere;
}
.confirm-s {
  color: var(--dim);
  font-size: 13px;
  margin-bottom: 18px;
}
.pop-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 30px;
  padding: 0 12px;
  border-radius: 8px;
  font-family: var(--sans);
  font-weight: 540;
  font-size: 13px;
  transition: background 0.12s, color 0.12s, opacity 0.12s;
}
/* Ring drawn inside the 30px box, so a focused button keeps its size and line. */
.btn:focus-visible {
  outline: 2px solid var(--purple);
  outline-offset: -2px;
}
.btn.ghost {
  color: var(--dim);
  box-shadow: inset 0 0 0 1px var(--border);
}
.btn.ghost:hover {
  background: color-mix(in srgb, var(--text) 6%, transparent);
  color: var(--text);
}
.btn.primary {
  background: var(--lime);
  /* text on a lime fill uses --ink, never --lime-ink, which is lime in dark */
  color: var(--ink);
  font-weight: 620;
}
.btn.primary:hover {
  filter: brightness(1.05);
}
</style>
