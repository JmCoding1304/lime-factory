<script setup lang="ts">
// The workspace shell at `#/`: top bar, three floating panels and the pane
// control. A port of prototype B's frame (dashboard/proto-workspace/b), reusing
// src/style.css tokens; every rule below is scoped.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { ComponentPublicInstance } from 'vue'
import {
  ALL,
  FOCUS_MAX,
  LEFT_MAX,
  PANES,
  PANE_MS,
  SAFE,
  computeDims,
  isAll,
  loadPanes,
  paneBindings,
  savePanes,
  togglePane,
} from '../lib/panes'
import type { Pane, PaneSet } from '../lib/panes'
import { useKeys, type KeyEventLike } from '../lib/keymap'
import { navigate, useRoute } from '../lib/router'
import { runTabName } from '../lib/editorTabs'
import { rememberFocus, shellEscape, type SettingsSection } from '../lib/settings'
import FileTree from './FileTree.vue'
import Editor from './Editor.vue'
import FocusTab from './FocusTab.vue'
import SettingsModal from './SettingsModal.vue'
import RunsSheet from './RunsSheet.vue'
import Notch from './Notch.vue'
import { notchRoom } from '../lib/notch'
import { ticketTarget, type FocusRow } from '../lib/focus'
import type { RunRef, SaveState } from '../lib/editorTabs'
import { live } from '../lib/live'
import type { RunSummary } from '../lib/types'

const props = defineProps<{
  projects: string[]
  runs: RunSummary[]
  loaded: boolean
  apiError: string | null
}>()

// ── Icons: the prototype's SVG helper (icons.js:50-52), paths copied per use ──
const PATHS: Record<string, string> = {
  layAll:
    '<rect width="18" height="16" x="3" y="4" rx="2.5"/><path d="M8.5 4v16"/><path d="M15.5 4v16"/>',
  layLeft:
    '<rect width="18" height="16" x="3" y="4" rx="2.5"/><path d="M9 4v16"/><rect x="4.6" y="5.6" width="2.8" height="12.8" rx=".9" fill="currentColor" stroke="none" opacity=".55"/>',
  layMiddle:
    '<rect width="18" height="16" x="3" y="4" rx="2.5"/><path d="M8.5 4v16"/><path d="M15.5 4v16"/><rect x="10" y="5.6" width="4" height="12.8" rx=".9" fill="currentColor" stroke="none" opacity=".55"/>',
  layRight:
    '<rect width="18" height="16" x="3" y="4" rx="2.5"/><path d="M13 4v16"/><rect x="14.6" y="5.6" width="4.8" height="12.8" rx="1" fill="currentColor" stroke="none" opacity=".55"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  settings:
    '<path d="M20 7h-9"/><path d="M14 17H5"/><circle cx="17" cy="17" r="3"/><circle cx="7" cy="7" r="3"/>',
  anchor:
    '<path d="M12 22V8"/><path d="M5 12H2a10 10 0 0 0 20 0h-3"/><circle cx="12" cy="5" r="3"/>',
  target:
    '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/>',
  folder:
    '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
  fileText:
    '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
}

function icon(name: string, size = 16, sw = 1.75): string {
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${PATHS[name] ?? ''}</svg>`
}

// ── Pane state ────────────────────────────────────────────────────────────────
const panes = ref<PaneSet>(loadPanes(localStorage))
const layoutWidth = ref(window.innerWidth - 16)
const d = computed(() => computeDims(layoutWidth.value, panes.value))
const cols = computed(() => d.value.tracks.map((x) => x + 'px').join(' '))
const all = computed(() => isAll(panes.value))
const paneKey = computed(() =>
  PANES.filter((k) => panes.value[k])
    .map((k) => k[0])
    .join(''),
)

const moving = ref(false)
const noAnim = ref(false)
const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)')

// Template refs replace the prototype's querySelector lookups.
const panelEls: Record<Pane, HTMLElement | null> = { left: null, middle: null, right: null }
const innerEls: Record<Pane, HTMLElement | null> = { left: null, middle: null, right: null }
function setPanelEl(k: Pane, el: Element | ComponentPublicInstance | null) {
  panelEls[k] = (el as HTMLElement) ?? null
}
function setInnerEl(k: Pane, el: Element | ComponentPublicInstance | null) {
  innerEls[k] = (el as HTMLElement) ?? null
}
const colsEl = ref<HTMLElement | null>(null)
const segEl = ref<HTMLElement | null>(null)

// ── Pane engine (port of app.js:381-425) ─────────────────────────────────────
const frozen = new Set<HTMLElement>()
let animTimer: ReturnType<typeof setTimeout> | null = null
let resizeTimer: ReturnType<typeof setTimeout> | null = null

function freeze(el: HTMLElement | null, width: number, anchor: 'left' | 'right') {
  if (!el) return
  frozen.add(el)
  el.style.width = width + 'px'
  if (anchor === 'left') el.style.right = 'auto'
  else el.style.left = 'auto'
}

function unfreeze() {
  if (animTimer) clearTimeout(animTimer)
  animTimer = null
  for (const el of frozen)
    for (const k of ['width', 'left', 'right', 'opacity', 'transition']) el.style.removeProperty(k)
  frozen.clear()
  moving.value = false
}

function crossfade(
  el: HTMLElement | null,
  fromW: number,
  toW: number,
  anchor: 'left' | 'right',
) {
  if (!el) return
  freeze(el, fromW, anchor)
  el.style.transition = 'opacity 110ms ease-out'
  el.style.opacity = '0'
  setTimeout(() => {
    if (!frozen.has(el)) return
    el.style.width = toW + 'px' // the one re-layout happens while invisible
    el.style.transition = 'opacity 200ms ease-out'
    el.style.opacity = '1'
  }, 120)
}

function setPanes(next: PaneSet) {
  if (PANES.every((k) => panes.value[k] === next[k])) return
  unfreeze()
  const from: Record<Pane, number> = { left: 0, middle: 0, right: 0 }
  for (const k of PANES) {
    const el = panelEls[k]
    from[k] = el ? el.getBoundingClientRect().width : 0
  }
  const prev = { ...panes.value }
  panes.value = { ...next }
  savePanes(localStorage, next)
  if (reduceMotion.matches || !panelEls.left) return
  const to = computeDims(layoutWidth.value, next).w
  moving.value = true
  for (const k of PANES) {
    const a = prev[k]
    const b = next[k]
    if (!a && !b) continue
    if (a !== b) {
      const wv = a ? from[k] : to[k]
      if (k === 'left') freeze(panelEls.left, wv, 'right') // drawer slides at the left edge
      else if (k === 'right') freeze(panelEls.right, wv, 'left') // drawer slides at the right edge
      else freeze(innerEls.middle, wv, 'left') // middle: content holds still, edge sweeps
    } else if (Math.abs(from[k] - to[k]) > 1 && Math.min(from[k], to[k]) < SAFE[k]) {
      crossfade(innerEls[k], from[k], to[k], k === 'right' ? 'right' : 'left')
    }
  }
  animTimer = setTimeout(unfreeze, PANE_MS + 40)
}

function showAll() {
  setPanes(ALL)
}

function toggle(k: Pane) {
  const next = togglePane(panes.value, k)
  if (next === null) {
    // the last pane stays: only shake the control, write nothing
    const seg = segEl.value
    if (seg) {
      seg.classList.remove('deny')
      void seg.offsetWidth
      seg.classList.add('deny')
    }
    return
  }
  setPanes(next)
}

// The buttons and ⌥1–4 share one set of actions, plus the shell's one Escape.
useKeys([...paneBindings({ showAll, toggle }), shellEscape(closeTop)])

// ── Resize (port of app.js:1752-1756) ────────────────────────────────────────
let ro: ResizeObserver | null = null
onMounted(() => {
  const el = colsEl.value
  if (!el) return
  ro = new ResizeObserver(([entry]) => {
    const nw = Math.round(entry.contentRect.width)
    if (Math.abs(nw - layoutWidth.value) > 1) {
      unfreeze()
      noAnim.value = true
      layoutWidth.value = nw
      if (resizeTimer) clearTimeout(resizeTimer)
      resizeTimer = setTimeout(() => (noAnim.value = false), 120)
    }
  })
  ro.observe(el)
})

// The notch is centred, so it only gets the room the two top-bar sides leave
// free. Track their widths from the real geometry on mount and on every resize.
const tbLeftEl = ref<HTMLElement | null>(null)
const tbRightEl = ref<HTMLElement | null>(null)
const notchSide = ref(420)
const notchRoomPx = computed(() => notchRoom(layoutWidth.value, notchSide.value))
let sidesRo: ResizeObserver | null = null
function measureSides() {
  const l = tbLeftEl.value
  const r = tbRightEl.value
  if (!l || !r) return
  notchSide.value = Math.max(
    l.getBoundingClientRect().right,
    window.innerWidth - r.getBoundingClientRect().left,
  )
}
onMounted(() => {
  measureSides()
  sidesRo = new ResizeObserver(measureSides)
  if (tbLeftEl.value) sidesRo.observe(tbLeftEl.value)
  if (tbRightEl.value) sidesRo.observe(tbRightEl.value)
})
onBeforeUnmount(() => {
  sidesRo?.disconnect()
})

onBeforeUnmount(() => {
  ro?.disconnect()
  if (animTimer) clearTimeout(animTimer)
  if (resizeTimer) clearTimeout(resizeTimer)
})

// ── Project, menus, save state, tabs, navigation ─────────────────────────────
// The project pill's sentinel for the all-projects view — Cost's default before
// the pages moved into Settings, kept reachable from the same switcher.
const PROJECT_ALL = 'all'
const project = ref<string | null>(null)
const projectOptions = computed(() => [PROJECT_ALL, ...props.projects])
watch(
  () => props.projects,
  (list) => {
    // 'all' is not a real project; never let the list reset it to project[0].
    if (
      project.value !== PROJECT_ALL &&
      (project.value === null || !list.includes(project.value))
    ) {
      project.value = list[0] ?? null
    }
  },
  { immediate: true },
)

// The file the tree last opened drives the editor; the top bar reads its state.
const editorEl = ref<InstanceType<typeof Editor> | null>(null)
const notchEl = ref<InstanceType<typeof Notch> | null>(null)
// The top bar's notch reads the live `running` frame directly; the template
// unwraps this ShallowRef.
const runningFrame = live.running
const fileTreeEl = ref<InstanceType<typeof FileTree> | null>(null)
const focusTabEl = ref<InstanceType<typeof FocusTab> | null>(null)
const activePath = ref<string | null>(null)
const saveState = ref<SaveState>(null)
function openFile(path: string) {
  editorEl.value?.open(path)
}

/** Open a report row's ticket: the shell's `project` is written from the guard
 *  target alone, so an unregistered or ticket-less row does nothing. */
async function openTicket(row: FocusRow) {
  const t = ticketTarget(props.projects, row)
  if (!t) return
  project.value = t.project
  await nextTick()
  editorEl.value?.open(t.path)
}

function dirtyUnder(p: string): string[] {
  return editorEl.value?.dirtyUnder(p) ?? []
}
function onRenamed(from: string, to: string) {
  editorEl.value?.renamePath(from, to)
}
function onTrashed(p: string) {
  editorEl.value?.closePath(p)
}
function onEditorState(s: { path: string | null; save: SaveState }) {
  activePath.value = s.path
  saveState.value = s.save
}

// The Editor's open Run tabs feed the live feed's subscribed set (its cursors).
function onRunTabs(refs: RunRef[]) {
  live.setRuns(refs)
}

/** Open the Run's detail tab in the current project's strip and reveal the
 *  middle pane. Never switches project. */
function showRun(runProject: string, runId: string) {
  const s = props.runs.find((r) => r.project === runProject && r.run_id === runId)
  editorEl.value?.openRun({ project: runProject, runId }, runTabName(s?.ticket, runId))
  if (!panes.value.middle) {
    const next = togglePane(panes.value, 'middle')
    if (next) setPanes(next)
  }
}

/** The in-app path: show the Run tab and put it in the hash so the link is
 *  shareable. Never switches project. */
function openRun(runProject: string, runId: string) {
  runsOpen.value = false
  showRun(runProject, runId)
  navigate(runProject, runId)
}

// A Run link holds until the editor has a project, then opens once; it is never
// re-opened on a later project switch.
const route = useRoute()
let pendingRun: { project: string; runId: string } | null = null
async function flushRun() {
  if (!pendingRun || !project.value || !editorEl.value) return
  // The editor mounts with project=null and only receives the updated prop after
  // the parent's render, so wait for that prop update before opening (a pre-flush
  // call would hit Editor.openRun's `!props.project` guard and drop the link).
  await nextTick()
  if (!pendingRun || !project.value || !editorEl.value) return
  const r = pendingRun
  pendingRun = null
  showRun(r.project, r.runId)
}
watch(
  route,
  (r) => {
    pendingRun = r.project && r.runId ? { project: r.project, runId: r.runId } : null
    void flushRun()
  },
  { immediate: true },
)
watch([project, editorEl], () => void flushRun(), { flush: 'post' })

const menu = ref<null | 'project'>(null)
const menuPos = ref({ left: 0, top: 0 })
const menuStyle = computed(() => ({ left: menuPos.value.left + 'px', top: menuPos.value.top + 'px' }))

// ── Settings modal ─────────────────────────────────────────────────────────────
const settingsOpen = ref(false)
const settingsSection = ref<SettingsSection>('cost') // kept across opens, like the prototype
const settingsEl = ref<InstanceType<typeof SettingsModal> | null>(null)
let restoreFocus: (() => void) | null = null

function openProjects(ev: MouseEvent) {
  if (!props.projects.length) return // an empty list disables the menu
  if (menu.value === 'project') {
    menu.value = null
    return
  }
  const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect()
  menuPos.value = { left: rect.left, top: rect.bottom + 6 }
  menu.value = 'project'
}

function openSettings() {
  if (settingsOpen.value) {
    void closeSettings()
    return
  }
  menu.value = null
  runsOpen.value = false
  restoreFocus = rememberFocus()
  settingsOpen.value = true
}

async function closeSettings() {
  settingsOpen.value = false
  await nextTick()
  restoreFocus?.()
  restoreFocus = null
}

/** The shell's top-of-stack Escape: the settings modal, then the Runs sheet,
 *  then the file tree's row menu or Trash confirm, then the editor's
 *  close-confirm, then the Focus card or project picker. */
function closeTop(e: KeyEventLike): boolean {
  if (settingsOpen.value) {
    if (settingsEl.value?.escape(e)) return true
    void closeSettings()
    return true
  }
  if (runsOpen.value) {
    runsOpen.value = false
    return true
  }
  if (notchEl.value?.escape()) return true
  return (
    (fileTreeEl.value?.escape() ?? false) ||
    (editorEl.value?.escape() ?? false) ||
    (focusTabEl.value?.escape() ?? false)
  )
}

function pickProject(p: string) {
  project.value = p
  menu.value = null
}

const runsOpen = ref(false)
function toggleRuns() {
  menu.value = null
  runsOpen.value = !runsOpen.value
}

const rightTab = ref<'focus' | 'firstmate'>('focus')

const paneBtns: { id: Pane; icon: string; title: string }[] = [
  { id: 'left', icon: 'layLeft', title: 'Left pane  ⌥2' },
  { id: 'middle', icon: 'layMiddle', title: 'Middle pane  ⌥3' },
  { id: 'right', icon: 'layRight', title: 'Right pane  ⌥4' },
]
</script>

<template>
  <div
    class="ws"
    :class="[`panes-${paneKey}`, { 'no-anim': noAnim, 'panes-moving': moving }]"
    :style="{ '--gL': d.gL + 'px', '--gR': d.gR + 'px' }"
  >
    <header class="topbar">
      <div ref="tbLeftEl" class="tb-left">
        <span class="brand" title="lime dash">
          <img class="brand-mark" src="/logo.svg" alt="" width="21" height="21" />
          <span><b>lime</b><span class="brand-rest">factory</span></span>
        </span>
        <button
          class="proj-pill"
          :class="{ on: menu === 'project' }"
          :disabled="!projects.length"
          @click.stop="openProjects"
        >
          <span class="proj-name">{{ project === PROJECT_ALL ? 'All projects' : project ?? 'no projects' }}</span>
          <span class="proj-chev" v-html="icon('chevronDown', 13, 2)" />
        </button>
        <div ref="segEl" class="seg lay-seg" role="group" aria-label="Panes">
          <button
            :class="{ on: all }"
            :aria-pressed="all ? 'true' : 'false'"
            title="All panes  ⌥1"
            data-pane="all"
            @click="showAll"
            v-html="icon('layAll', 17, 1.6)"
          />
          <span class="lay-sep" />
          <button
            v-for="p in paneBtns"
            :key="p.id"
            :data-pane="p.id"
            :class="{ on: !all && panes[p.id], in: all }"
            :aria-pressed="panes[p.id] ? 'true' : 'false'"
            :title="p.title"
            @click="toggle(p.id)"
            v-html="icon(p.icon, 17, 1.6)"
          />
        </div>
      </div>

      <Notch ref="notchEl" :frame="runningFrame" :room="notchRoomPx" @open-run="openRun" />

      <div ref="tbRightEl" class="tb-right">
        <span
          v-if="saveState"
          class="save-state"
          :class="`ss-${saveState}`"
        >
          <template v-if="saveState === 'dirty'"
            ><span class="ss-dot" />Unsaved<kbd>⌘S</kbd></template
          >
          <template v-else
            ><span v-html="icon('check', 13, 2.2)" />Saved</template
          >
        </span>
        <span class="tb-sep" />
        <button
          class="tb-btn"
          :class="{ on: runsOpen }"
          aria-haspopup="dialog"
          @click.stop="toggleRuns"
        >
          <span v-html="icon('activity', 15, 1.8)" />Runs
        </button>
        <button
          class="tb-btn icon-only"
          :class="{ on: settingsOpen }"
          title="Settings"
          aria-haspopup="dialog"
          @click.stop="openSettings"
          v-html="icon('settings', 16, 1.8)"
        />
      </div>
    </header>

    <main ref="colsEl" class="cols" :style="{ gridTemplateColumns: cols }">
      <section class="col col-left" :aria-hidden="!panes.left">
        <div class="panel" :ref="(el) => setPanelEl('left', el)">
          <div class="pane-inner" :ref="(el) => setInnerEl('left', el)">
            <div class="pane-cap" :style="{ maxWidth: LEFT_MAX + 'px' }">
              <FileTree
                v-if="project"
                ref="fileTreeEl"
                :project="project"
                :active="activePath"
                :dirty-under="dirtyUnder"
                @open="openFile"
                @renamed="onRenamed"
                @trashed="onTrashed"
              />
              <div v-else class="empty">
                <div class="empty-glyph" v-html="icon('folder', 22, 1.5)" />
                <div class="empty-title">No projects</div>
                <div class="empty-sub">Registered projects' files list here.</div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section class="col col-middle" :aria-hidden="!panes.middle">
        <div class="panel" :ref="(el) => setPanelEl('middle', el)">
          <div class="pane-inner" :ref="(el) => setInnerEl('middle', el)">
            <Editor ref="editorEl" :project="project" @state="onEditorState" @runs="onRunTabs" />
            <div v-if="!activePath" class="empty">
              <div class="empty-glyph" v-html="icon('fileText', 22, 1.5)" />
              <div class="empty-title">No file open</div>
              <div class="empty-sub">Files you open will show here as tabs.</div>
            </div>
          </div>
        </div>
      </section>

      <section class="col col-right" :aria-hidden="!panes.right">
        <div class="panel" :ref="(el) => setPanelEl('right', el)">
          <div class="pane-inner" :ref="(el) => setInnerEl('right', el)">
            <div class="right">
              <div class="pane-head rp-head">
                <div class="pill-tabs" :class="{ 'at-firstmate': rightTab === 'firstmate' }">
                  <span class="pill-thumb" />
                  <button :class="{ on: rightTab === 'focus' }" @click="rightTab = 'focus'">
                    Focus
                  </button>
                  <button :class="{ on: rightTab === 'firstmate' }" @click="rightTab = 'firstmate'">
                    firstmate
                  </button>
                </div>
              </div>
              <div class="rp-body">
                <div
                  v-show="rightTab === 'focus'"
                  class="pane-cap"
                  :style="{ maxWidth: FOCUS_MAX + 'px' }"
                >
                  <FocusTab
                    ref="focusTabEl"
                    :project="project === PROJECT_ALL ? null : project"
                    @open-row="openTicket"
                    @open-run="openRun"
                  />
                </div>
                <div v-show="rightTab === 'firstmate'" class="firstmate">
                  <div class="fm-center">
                    <div class="fm-glyph" v-html="icon('anchor', 20, 1.6)" />
                    <div class="fm-title">firstmate is coming</div>
                    <div class="fm-sub">The durable agent will work beside you here.</div>
                  </div>
                  <div class="fm-input" aria-disabled="true"><span>Message firstmate</span></div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>
    </main>

    <div v-if="menu" class="catcher" @mousedown="menu = null" />

    <div v-if="menu === 'project'" class="pop menu" :style="menuStyle">
      <button
        v-for="p in projectOptions"
        :key="p"
        class="menu-item"
        :class="{ on: p === project }"
        @click="pickProject(p)"
      >
        <span class="menu-label">{{ p === PROJECT_ALL ? 'All projects' : p }}</span>
        <span v-if="p === project" class="menu-check" v-html="icon('check', 14, 2.2)" />
      </button>
    </div>

    <Transition name="set-fade">
      <SettingsModal
        v-if="settingsOpen"
        ref="settingsEl"
        v-model:section="settingsSection"
        :project="project"
        :projects="projects"
        @close="closeSettings"
      />
    </Transition>

    <Transition name="rs" :duration="{ enter: 190, leave: 140 }">
      <RunsSheet
        v-if="runsOpen"
        :runs="runs"
        :loaded="loaded"
        :api-error="apiError"
        @close="runsOpen = false"
        @open="openRun"
      />
    </Transition>
  </div>
</template>

<style scoped>
.ws {
  /* token map onto the dashboard theme */
  --panel: var(--surface);
  --panel-2: var(--surface-2);
  --muted: var(--dim);
  --line: var(--border);
  --line-soft: var(--border);
  --raised: var(--surface);
  --c-needs: var(--warn);
  --hover: color-mix(in srgb, var(--text) 5%, transparent);
  --press: color-mix(in srgb, var(--text) 8%, transparent);
  --r-panel: 12px;
  --r-ctl: 8px;
  --ease-pane: cubic-bezier(0.32, 0.72, 0, 1);
  --dur-pane: 340ms;
  --ease: cubic-bezier(0.2, 0.8, 0.2, 1);
  --shadow-pop: 0 4px 12px rgba(26, 26, 26, 0.1), 0 16px 40px rgba(26, 26, 26, 0.14);
  height: 100vh;
  overflow: hidden;
  display: grid;
  grid-template-rows: 52px minmax(0, 1fr);
  background: var(--bg);
}

/* ── frame and top bar (styles.css:118-200) ─────────────────────────────────── */
.topbar {
  position: relative;
  z-index: 35;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 12px 0 16px;
  gap: 12px;
}
.tb-left,
.tb-right {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.tb-right {
  gap: 6px;
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  letter-spacing: 0.01em;
  white-space: nowrap;
  margin-right: 6px;
  user-select: none;
}
.brand-mark {
  width: 21px;
  height: 21px;
  display: block;
}
.brand b {
  font-weight: 700;
}
.brand-rest {
  font-weight: 300;
  color: var(--muted);
}
.tb-sep {
  width: 1px;
  height: 18px;
  background: var(--line);
  margin: 0 4px;
}
.proj-pill {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 10px 0 12px;
  border-radius: 999px;
  background: var(--panel);
  box-shadow: var(--shadow);
  font: inherit;
  font-weight: 560;
  letter-spacing: -0.005em;
  transition: background 0.12s;
}
.proj-pill:hover,
.proj-pill.on {
  background: var(--panel-2);
}
.proj-pill:disabled {
  color: var(--faint);
  cursor: default;
}
.proj-chev {
  color: var(--muted);
  transition: transform 0.16s var(--ease);
}
.proj-pill.on .proj-chev {
  transform: rotate(180deg);
}
.seg {
  position: relative;
  display: inline-flex;
  padding: 2px;
  border-radius: 9px;
  background: var(--hover);
}
.seg > button {
  position: relative;
  z-index: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--muted);
  border-radius: 7px;
  transition: color 0.12s;
}
.seg > button:hover {
  color: var(--text);
}
.seg > button.on {
  color: var(--text);
}
.lay-seg {
  margin-left: 4px;
  gap: 1px;
}
.lay-seg > button {
  width: 30px;
  height: 26px;
  transition: color 0.12s, background 0.16s, box-shadow 0.16s;
}
.lay-seg > button.on {
  background: var(--panel);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08), 0 0 0 0.5px var(--line);
}
.lay-seg > button.in {
  color: var(--text);
}
.lay-seg > .lay-sep {
  width: 1px;
  margin: 6px 3px;
  background: var(--line);
}
.lay-seg.deny {
  animation: deny 0.28s var(--ease);
}
@keyframes deny {
  25% {
    transform: translateX(-3px);
  }
  50% {
    transform: translateX(3px);
  }
  75% {
    transform: translateX(-1.5px);
  }
}
.tb-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 10px;
  border-radius: var(--r-ctl);
  color: var(--muted);
  font-weight: 500;
  transition: background 0.12s, color 0.12s;
}
.tb-btn:hover,
.tb-btn.on {
  background: var(--hover);
  color: var(--text);
}
.tb-btn.icon-only {
  width: 30px;
  padding: 0;
  justify-content: center;
}
.save-state {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--faint);
  margin-right: 6px;
  white-space: nowrap;
}
.save-state.ss-dirty {
  color: var(--muted);
}
.save-state kbd {
  margin-left: 2px;
}
.ss-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--c-needs);
}

/* ── columns and panels (styles.css:161-200) ────────────────────────────────── */
.cols {
  display: grid;
  padding: 0 8px 8px;
  min-height: 0;
  transition: grid-template-columns var(--dur-pane) var(--ease-pane);
}
.no-anim .cols,
.no-anim .panel {
  transition: none !important;
}
.col {
  position: relative;
  min-width: 0;
  overflow: hidden;
}
.panel {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  right: 0;
  background: var(--panel);
  border-radius: var(--r-panel);
  box-shadow: var(--shadow);
  overflow: hidden;
  transition: left var(--dur-pane) var(--ease-pane), right var(--dur-pane) var(--ease-pane);
}
.col-left .panel {
  right: var(--gL, 8px);
}
.col-right .panel {
  left: var(--gR, 8px);
}
.pane-inner {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  right: 0;
  display: flex;
  flex-direction: column;
}
.pane-inner > * {
  flex: 1;
  min-height: 0;
}
.pane-cap {
  position: relative;
  width: 100%;
  max-width: 560px;
  margin: 0 auto;
  flex: 1;
  min-height: 0;
}
.pane-head {
  height: 46px;
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 10px 0 16px;
}
.pane-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
  letter-spacing: 0.01em;
}

/* ── empty states (styles.css:305-308) ──────────────────────────────────────── */
.empty {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  text-align: center;
  padding: 24px;
}
.empty-glyph {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--faint);
  background: var(--panel-2);
  margin-bottom: 4px;
}
.empty-title {
  font-size: 14px;
  font-weight: 560;
  color: var(--muted);
}
.empty-sub {
  font-size: 12.5px;
  color: var(--faint);
  max-width: 44ch;
  overflow-wrap: anywhere;
}

/* ── right pane (styles.css:340-350) ────────────────────────────────────────── */
.right {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}
.rp-head {
  padding: 0 10px;
  justify-content: center;
}
.pill-tabs {
  position: relative;
  display: inline-flex;
  padding: 3px;
  border-radius: 10px;
  background: var(--hover);
}
.pill-tabs button {
  position: relative;
  z-index: 1;
  width: 112px;
  height: 26px;
  border-radius: 7px;
  font-size: 12.5px;
  font-weight: 540;
  color: var(--muted);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  transition: color 0.12s;
}
.pill-tabs button.on {
  color: var(--text);
}
.pill-thumb {
  position: absolute;
  top: 3px;
  left: 3px;
  width: 112px;
  height: 26px;
  border-radius: 7px;
  background: var(--panel);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08), 0 0 0 0.5px var(--line);
  transition: transform 0.17s var(--ease);
}
.pill-tabs.at-firstmate .pill-thumb {
  transform: translateX(112px);
}
.rp-body {
  flex: 1;
  min-height: 0;
  position: relative;
  display: flex;
  flex-direction: column;
}

/* ── firstmate (styles.css:395-400) ─────────────────────────────────────────── */
.firstmate {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  padding: 0 14px 14px;
}
.fm-center {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  text-align: center;
}
.fm-glyph {
  width: 44px;
  height: 44px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--muted);
  background: var(--panel-2);
  box-shadow: inset 0 0 0 1px var(--line-soft);
  margin-bottom: 8px;
}
.fm-title {
  font-size: 14px;
  font-weight: 580;
}
.fm-sub {
  font-size: 12.5px;
  color: var(--faint);
}
.fm-input {
  height: 40px;
  border-radius: 10px;
  border: 1px dashed var(--line);
  display: flex;
  align-items: center;
  padding: 0 14px;
  color: var(--faint);
  font-size: 13px;
  max-width: 680px;
  width: 100%;
  margin: 0 auto;
}

/* ── menus (styles.css:405, 507-516) ────────────────────────────────────────── */
.catcher {
  position: fixed;
  inset: 0;
  z-index: 55;
}
.pop {
  position: fixed;
  z-index: 60;
  background: var(--raised);
  border-radius: 12px;
  box-shadow: var(--shadow-pop);
  padding: 6px;
}
.menu {
  min-width: 200px;
}
.menu-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 9px;
  height: 32px;
  padding: 0 10px;
  border-radius: 7px;
  color: var(--text);
  transition: background 0.08s;
}
.menu-item:hover {
  background: var(--hover);
}
.menu-item > span:first-child {
  color: var(--muted);
}
.menu-label {
  flex: 1;
  text-align: left;
  color: var(--text) !important;
}
.menu-check {
  color: var(--lime-ink);
}

/* ── settings modal fade (opacity only) ──────────────────────────────────────── */
.set-fade-enter-active,
.set-fade-leave-active {
  transition: opacity 0.14s ease;
}
.set-fade-enter-from,
.set-fade-leave-to {
  opacity: 0;
}

/* ── reduced motion (styles.css:828) ────────────────────────────────────────── */
@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    transition-duration: 1ms !important;
    animation-duration: 1ms !important;
    animation-iteration-count: 1 !important;
  }
}
</style>
