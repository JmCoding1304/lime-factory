<script setup lang="ts">
// The Focus tab's body: lime's ranked home report and goals as one to-do list
// (ticket 238). A port of prototype B's FocusList/FocusRow (proto-focus-runs/
// app.js), wired to the server's home proxies. The shell owns keys; this
// component binds none, and exposes `escape` so the shell can close its card
// or project picker.
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { fetchHomeFocus, postHomeTick, postHomeVisit } from '../lib/api'
import {
  KIND_LABEL,
  TICK_MS,
  applyFocus,
  cardButtons,
  copiedLine,
  doneLabel,
  errorLine,
  focusEscape,
  focusList,
  goalLine,
  loadFocusProject,
  loadHome,
  pickerChoice,
  pickerMove,
  pickerOpenIndex,
  pickerQueryIndex,
  projectOptions,
  pushLine,
  reconcileTicks,
  rowStep,
  saveFocusProject,
  settleTick,
  startTick,
  summaryView,
  tickRequest,
  todoRows,
  type CardButton,
  type FocusRow,
  type HomeState,
  type Ticks,
} from '../lib/focus'
import type { HomeTickResponse } from '../lib/types'
import { fmtWhen } from '../lib/format'
import { live } from '../lib/live'

const props = defineProps<{ project: string | null }>()
const emit = defineEmits<{
  openRow: [row: FocusRow]
  openRun: [project: string, runId: string]
}>()

const state = ref<HomeState>({ report: null, goals: null, errors: [], initLine: null })
const loading = ref(true)
const ticks = ref<Ticks>({})
const holding = ref<string[]>([])
const filter = ref<string | null>(loadFocusProject(localStorage))
const openKey = ref<string | null>(null)
const doneOpen = ref(false)
const writeError = ref<string | null>(null)
const note = ref<string | null>(null)

// The project picker: a teleported popup below the header button.
const pickerOpen = ref(false)
const query = ref('')
const active = ref(0)
const pickerPos = ref({ left: 0, top: 0 })
const listEl = ref<HTMLElement | null>(null)
const fprojBtn = ref<HTMLButtonElement | null>(null)
const fpopIn = ref<HTMLInputElement | null>(null)

// One load per Workspace mount, then live updates over the live feed,
// lib/live.ts; a reconnect resends Focus. The right-pane bodies use v-show, so
// switching to firstmate and back neither reloads nor re-visits.
let stop: (() => void) | null = null
let unmounted = false

onMounted(async () => {
  state.value = await loadHome({ visit: postHomeVisit, focus: fetchHomeFocus })
  loading.value = false
  if (unmounted) return
  stop = watch(live.focus, (res) => {
    if (!res) return
    state.value = applyFocus(state.value, res)
    ticks.value = reconcileTicks(ticks.value, state.value.report, state.value.goals)
  })
})

onUnmounted(() => {
  unmounted = true
  stop?.()
})

const rows = computed(() =>
  todoRows(state.value.report, state.value.goals, ticks.value, holding.value),
)
const list = computed(() => focusList(rows.value, props.project, filter.value))
const options = computed(() =>
  projectOptions(rows.value.open, props.project, filter.value, query.value),
)
const done = computed(() => doneLabel(list.value.done.length))
const push = computed(() => pushLine(state.value.report?.home ?? null))
const err = computed(() => errorLine(state.value.errors))

// The open rows, then an "Other projects" divider, then the rest — one markup
// body renders every section, so it is never pasted twice.
const sections = computed(() => {
  const parts: { title: string | null; rows: FocusRow[] }[] = []
  const { here, elsewhere } = list.value
  if (here.length) parts.push({ title: null, rows: here })
  if (here.length && elsewhere.length) parts.push({ title: 'Other projects', rows: elsewhere })
  else if (elsewhere.length) parts.push({ title: null, rows: elsewhere })
  return parts
})

function cardId(key: string): string {
  return 'card-' + key.replace(/[^a-z0-9]/gi, '_')
}

function checked(row: FocusRow): boolean {
  return ticks.value[row.key]?.done ?? row.done
}

function dueText(row: FocusRow): string {
  if (row.kind !== 'goal' || !row.due) return ''
  return row.overdue ? 'overdue' : row.due
}

function sinceText(row: FocusRow): string | null {
  return row.since ? 'since ' + fmtWhen(row.since, Date.now()) : null
}

function toggle(row: FocusRow) {
  openKey.value = openKey.value === row.key ? null : row.key
}

async function setDone(row: FocusRow, done: boolean) {
  const req = tickRequest(row, done)
  if (!req) return
  if (ticks.value[row.key]?.phase === 'writing') return
  writeError.value = null
  ticks.value = startTick(ticks.value, row, done, Date.now())
  if (done) {
    holding.value = [...holding.value, row.key]
    setTimeout(() => {
      holding.value = holding.value.filter((k) => k !== row.key)
    }, TICK_MS)
    if (openKey.value === row.key) openKey.value = null
  }
  let res: HomeTickResponse
  try {
    res = await postHomeTick(req)
  } catch (e) {
    res = { ok: false, message: e instanceof Error ? e.message : String(e) }
  }
  const out = settleTick(ticks.value, row.key, res)
  ticks.value = out.ticks
  writeError.value = out.error
}

function press(row: FocusRow, b: CardButton) {
  if (b.kind === 'open-ticket') {
    emit('openRow', row)
    return
  }
  if (b.kind === 'open-run') {
    if (row.run_id && row.project) emit('openRun', row.project, row.run_id)
    return
  }
  if (b.kind === 'mark-done') {
    void setDone(row, true)
    return
  }
  if (b.kind === 'command' && b.command) {
    const cmd = b.command
    navigator.clipboard.writeText(cmd).then(
      () => {
        note.value = copiedLine(cmd, row.project)
      },
      () => {
        note.value = "Couldn't copy — run it yourself: " + cmd
      },
    )
    setTimeout(() => {
      note.value = null
    }, 4000)
  }
}

function btnClass(b: CardButton): string {
  if (b.kind === 'open-ticket' || b.kind === 'open-run') return 'link'
  return b.primary ? 'primary' : 'ghost'
}

function step(dir: 1 | -1) {
  const el = listEl.value
  if (!el) return
  rowStep(el, document.activeElement, dir)?.focus()
}

function openPicker() {
  const el = fprojBtn.value
  if (el) {
    const r = el.getBoundingClientRect()
    pickerPos.value = { left: Math.round(r.left), top: Math.round(r.bottom + 6) }
  }
  query.value = ''
  pickerOpen.value = true
  active.value = pickerOpenIndex(options.value, filter.value)
  void nextTick(() => fpopIn.value?.focus())
}

// Every query change moves the highlight onto the narrowed list (ticket 243).
function setQuery(q: string) {
  query.value = q
  active.value = pickerQueryIndex(options.value)
}

function pick(id: string | null) {
  filter.value = id
  saveFocusProject(localStorage, id)
  openKey.value = null
  pickerOpen.value = false
  fprojBtn.value?.focus()
}

function pickerKey(e: KeyboardEvent) {
  const n = options.value.length
  if (e.key === 'ArrowDown') {
    active.value = pickerMove(active.value, n, 1)
    e.preventDefault()
  } else if (e.key === 'ArrowUp') {
    active.value = pickerMove(active.value, n, -1)
    e.preventDefault()
  } else if (e.key === 'Enter') {
    const o = pickerChoice(options.value, active.value)
    if (o) pick(o.id)
    e.preventDefault()
  } else if (e.key === 'Tab') {
    pickerOpen.value = false
  }
}

function escape(): boolean {
  const res = focusEscape({ open: openKey.value, picker: pickerOpen.value })
  if (res.closed === null) return false
  if (res.closed === 'picker') {
    pickerOpen.value = false
    fprojBtn.value?.focus()
  } else {
    const key = openKey.value
    openKey.value = null
    void nextTick(() => {
      if (key) {
        listEl.value?.querySelector<HTMLElement>(`[data-toggle="${CSS.escape(key)}"]`)?.focus()
      }
    })
  }
  return true
}

defineExpose({ escape })
</script>

<template>
  <div class="focus" ref="listEl" @keydown.down.prevent="step(1)" @keydown.up.prevent="step(-1)">
    <div class="fhead">
      <button
        class="fproj"
        ref="fprojBtn"
        aria-haspopup="listbox"
        :aria-expanded="pickerOpen"
        @click="openPicker"
      >
        <span class="fproj-k">Project:</span>
        <span class="fproj-v">{{ filter ?? 'All' }}</span>
        <svg
          class="fproj-chev"
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>
    </div>

    <div class="focus-inner">
      <div v-if="loading" class="f-loading">Loading…</div>
      <div v-if="err" class="f-error" :title="err">{{ err }}</div>
      <div v-if="state.initLine" class="f-warn">{{ state.initLine }}</div>
      <div v-if="push" class="f-warn">{{ push }}</div>
      <div v-if="writeError" class="f-error" :title="writeError">{{ writeError }}</div>
      <div v-if="note" class="f-warn">{{ note }}</div>

      <div v-for="(sec, si) in sections" :key="si">
        <div v-if="sec.title" class="fdivider"><span>{{ sec.title }}</span></div>
        <ul class="flist">
          <li
            v-for="row in sec.rows"
            :key="row.key"
            class="frow"
            :class="['k-' + row.kind, { open: openKey === row.key, struck: row.struck }]"
          >
            <div class="frow-main">
              <button
                v-if="row.checkable"
                class="cbox"
                :class="{ goal: row.kind === 'goal' }"
                role="checkbox"
                :aria-checked="checked(row)"
                :aria-label="(checked(row) ? 'Not done' : 'Done') + ': ' + row.title"
                @click="setDone(row, !checked(row))"
              >
                <svg
                  width="12"
                  height="12"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="3"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  aria-hidden="true"
                >
                  <path d="M20 6 9 17l-5-5" />
                </svg>
              </button>
              <span v-else class="glyph" :class="'g-' + row.kind" role="img" :aria-label="KIND_LABEL[row.kind]"><i /></span>
              <button
                class="f-hit"
                :data-toggle="row.key"
                :aria-expanded="openKey === row.key"
                :aria-controls="cardId(row.key)"
                @click="toggle(row)"
              >
                <span class="ftitle"><span v-if="row.num" class="fnum">{{ row.num }}</span>{{ row.title }}</span>
                <span v-if="dueText(row)" class="fdue" :class="{ overdue: row.overdue }">{{ dueText(row) }}</span>
                <span v-if="row.project" class="chip" :class="{ here: row.project === props.project }">{{ row.project }}</span>
              </button>
            </div>
            <div class="fcard" :id="cardId(row.key)" role="region" :inert="openKey !== row.key">
              <div>
                <div class="fc-in">
                  <template v-if="row.kind === 'goal'">
                    <div class="gc-line">{{ goalLine(row) }}</div>
                    <div class="fc-acts">
                      <button
                        v-for="b in cardButtons(row)"
                        :key="b.kind"
                        class="btn"
                        :class="btnClass(b)"
                        @click="press(row, b)"
                      >{{ b.label }}</button>
                    </div>
                  </template>
                  <template v-else>
                    <div class="fc-sum">
                      <div v-if="summaryView(row).state === 'writing'" class="fc-writing">
                        <span class="fc-wlabel"><span class="wdot" />writing…</span>
                        <span class="fc-skel" style="width: 92%" />
                        <span class="fc-skel" style="width: 64%" />
                      </div>
                      <p v-else-if="summaryView(row).state === 'text'" class="fc-text">{{ (summaryView(row) as { text: string }).text }}</p>
                      <p v-else class="fc-fail">Couldn't write a summary</p>
                    </div>
                    <div class="fc-acts">
                      <button
                        v-for="(b, bi) in cardButtons(row)"
                        :key="bi"
                        class="btn"
                        :class="btnClass(b)"
                        @click="press(row, b)"
                      >{{ b.label }}</button>
                      <span class="fc-meta">
                        {{ row.detail }}<template v-if="sinceText(row)"> · {{ sinceText(row) }}</template>
                      </span>
                    </div>
                  </template>
                </div>
              </div>
            </div>
          </li>
        </ul>
      </div>

      <div v-if="!loading && !sections.length" class="f-nothing">
        {{ filter === null ? 'Nothing needs you' : 'Nothing left in ' + filter }}
      </div>

      <div v-if="done" class="ffoot">
        <button class="fdone" :aria-expanded="doneOpen" aria-controls="done-list" @click="doneOpen = !doneOpen">
          {{ done }}
        </button>
        <ul v-if="doneOpen" class="flist done-list" id="done-list">
          <li
            v-for="row in list.done"
            :key="row.key"
            class="frow"
            :class="['k-' + row.kind, { open: openKey === row.key }]"
          >
            <div class="frow-main">
              <button
                v-if="row.checkable"
                class="cbox"
                :class="{ goal: row.kind === 'goal' }"
                role="checkbox"
                aria-checked="true"
                :aria-label="'Not done: ' + row.title"
                @click="setDone(row, false)"
              >
                <svg
                  width="12"
                  height="12"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="3"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  aria-hidden="true"
                >
                  <path d="M20 6 9 17l-5-5" />
                </svg>
              </button>
              <span v-else class="glyph g-done" role="img" aria-label="Done">
                <svg
                  width="15"
                  height="15"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2.6"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  aria-hidden="true"
                >
                  <path d="M20 6 9 17l-5-5" />
                </svg>
              </span>
              <button
                class="f-hit"
                :data-toggle="row.key"
                :aria-expanded="openKey === row.key"
                :aria-controls="cardId(row.key)"
                @click="toggle(row)"
              >
                <span class="ftitle"><span v-if="row.num" class="fnum">{{ row.num }}</span>{{ row.title }}</span>
                <span v-if="row.project" class="chip" :class="{ here: row.project === props.project }">{{ row.project }}</span>
              </button>
            </div>
            <div class="fcard" :id="cardId(row.key)" role="region" :inert="openKey !== row.key">
              <div>
                <div class="fc-in">
                  <template v-if="row.kind === 'goal'">
                    <div class="gc-line">{{ goalLine(row) }}</div>
                    <div class="fc-acts">
                      <button
                        v-for="b in cardButtons(row)"
                        :key="b.kind"
                        class="btn"
                        :class="btnClass(b)"
                        @click="press(row, b)"
                      >{{ b.label }}</button>
                    </div>
                  </template>
                  <template v-else>
                    <div class="fc-sum">
                      <div v-if="summaryView(row).state === 'writing'" class="fc-writing">
                        <span class="fc-wlabel"><span class="wdot" />writing…</span>
                        <span class="fc-skel" style="width: 92%" />
                        <span class="fc-skel" style="width: 64%" />
                      </div>
                      <p v-else-if="summaryView(row).state === 'text'" class="fc-text">{{ (summaryView(row) as { text: string }).text }}</p>
                      <p v-else class="fc-fail">Couldn't write a summary</p>
                    </div>
                    <div class="fc-acts">
                      <button
                        v-for="(b, bi) in cardButtons(row)"
                        :key="bi"
                        class="btn"
                        :class="btnClass(b)"
                        @click="press(row, b)"
                      >{{ b.label }}</button>
                      <span class="fc-meta">
                        {{ row.detail }}<template v-if="sinceText(row)"> · {{ sinceText(row) }}</template>
                      </span>
                    </div>
                  </template>
                </div>
              </div>
            </div>
          </li>
        </ul>
      </div>
    </div>
  </div>

  <Teleport to="body">
    <template v-if="pickerOpen">
      <div class="fpop-catcher" @mousedown="pickerOpen = false" />
      <div class="fpop" :style="{ left: pickerPos.left + 'px', top: pickerPos.top + 'px' }">
        <input
          ref="fpopIn"
          class="fpop-in"
          role="combobox"
          placeholder="Find a project"
          autocomplete="off"
          spellcheck="false"
          :value="query"
          @input="setQuery(($event.target as HTMLInputElement).value)"
          @keydown="pickerKey"
        />
        <ul class="fpop-list" role="listbox" aria-label="Projects">
          <li
            v-for="(o, i) in options"
            :key="i"
            class="fpop-opt"
            :class="{ active: i === active }"
            role="option"
            :aria-selected="o.id === filter"
            @mousedown.prevent
            @click="pick(o.id)"
            @mousemove="active = i"
          >
            <span class="fpop-check">{{ o.id === filter ? '✓' : '' }}</span>
            <span class="fpop-name">{{ o.label }}</span>
            <span class="fpop-n">{{ o.count }}</span>
          </li>
          <li v-if="!options.length" class="fpop-none">No project matches</li>
        </ul>
      </div>
    </template>
  </Teleport>
</template>

<style scoped>
.focus {
  --c-needs: var(--warn);
  --c-failed: var(--bad);
  --c-ready: var(--ok);
  --c-goal: var(--run);
  --accent: var(--lime-ink);
  --accent-soft: color-mix(in srgb, var(--lime) 18%, transparent);
  position: absolute;
  inset: 0;
  overflow: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--border) transparent;
}
.fhead { position: sticky; top: 0; z-index: 2; max-width: 680px; margin: 0 auto; padding: 0 10px 6px; background: var(--surface); }
.fproj { display: inline-flex; align-items: center; gap: 5px; height: 28px; padding: 0 8px 0 10px; border-radius: 8px; font: inherit; font-size: 12.5px; color: var(--dim); border: 1px solid var(--border); background: none; cursor: pointer; transition: background .12s, color .12s; }
.fproj:hover, .fproj[aria-expanded="true"] { background: var(--hover, color-mix(in srgb, var(--text) 5%, transparent)); color: var(--text); }
.fproj-k { color: var(--dim); }
.fproj-v { color: var(--text); font-weight: 600; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fproj-chev { color: var(--faint); transition: transform .16s cubic-bezier(.2,.8,.2,1); }
.fproj[aria-expanded="true"] .fproj-chev { transform: rotate(180deg); }
.focus-inner { max-width: 680px; margin: 0 auto; padding: 2px 10px 28px; }
.f-loading, .f-nothing { padding: 12px; font-size: 13px; color: var(--faint); }
.f-error { margin: 0 12px 6px; padding: 6px 10px; border-radius: 8px; background: var(--bad-chip); color: var(--bad); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.f-warn { margin: 0 12px 6px; font-size: 12px; color: var(--warn); }
.flist { list-style: none; margin: 0; padding: 0; }
.frow { border-radius: 9px; transition: background .12s; }
.frow:hover, .frow.open { background: var(--hover, color-mix(in srgb, var(--text) 5%, transparent)); }
.frow-main { display: flex; align-items: center; gap: 10px; height: 40px; padding: 0 8px 0 11px; }
.f-hit { flex: 1; min-width: 0; height: 100%; display: flex; align-items: center; gap: 10px; border-radius: 7px; cursor: pointer; font: inherit; color: inherit; background: none; border: 0; text-align: left; }
.f-hit:focus-visible { outline: none; box-shadow: inset 0 0 0 2px var(--accent-soft); }
.ftitle { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13.5px; }
.fnum { display: inline-block; min-width: 2.1em; color: var(--faint); font-variant-numeric: tabular-nums; margin-right: 5px; }
.fdue { flex: none; font-size: 12px; color: var(--faint); font-variant-numeric: tabular-nums; }
.fdue.overdue { color: var(--c-failed); font-weight: 560; }
.chip { flex: none; font-size: 11px; color: var(--dim); background: var(--surface-2); border: 1px solid var(--border); padding: 1px 7px; border-radius: 999px; max-width: 112px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chip.here { color: var(--accent); background: var(--accent-soft); border-color: transparent; }
.cbox { width: 17px; height: 17px; flex: none; border-radius: 5px; display: inline-flex; align-items: center; justify-content: center; padding: 0; background: none; border: 0; cursor: pointer; box-shadow: inset 0 0 0 1.5px var(--faint); color: transparent; transition: background .14s, box-shadow .14s, transform .08s; }
.cbox:hover { box-shadow: inset 0 0 0 1.5px var(--dim); }
.cbox:active { transform: scale(.9); }
.cbox.goal { border-radius: 50%; }
.cbox[aria-checked="true"] { background: var(--lime); box-shadow: none; color: var(--ink); }
.glyph { width: 17px; height: 17px; flex: none; display: inline-flex; align-items: center; justify-content: center; }
.glyph i { display: block; }
.g-failed i { width: 8px; height: 8px; background: var(--c-failed); border-radius: 2px; transform: rotate(45deg); }
.g-ready i { width: 9px; height: 9px; background: var(--c-ready); border-radius: 50%; box-shadow: 0 0 0 3px color-mix(in srgb, var(--c-ready) 18%, transparent); }
.g-done { color: var(--c-ready); }
.frow.struck .ftitle { color: var(--faint); text-decoration: line-through; text-decoration-color: var(--faint); }
.frow.struck .fnum { color: var(--faint); }
.fcard { display: grid; grid-template-rows: 0fr; transition: grid-template-rows .2s cubic-bezier(.2,.8,.2,1); }
.fcard > div { min-height: 0; overflow: hidden; }
.frow.open > .fcard { grid-template-rows: 1fr; }
.fc-in { padding: 0 14px 13px 38px; opacity: 0; transform: translateY(-3px); transition: opacity .16s ease-out, transform .18s cubic-bezier(.2,.8,.2,1); }
.frow.open > .fcard .fc-in { opacity: 1; transform: none; transition-delay: .04s; }
.fc-text { margin: 0; font-size: 13px; line-height: 1.55; color: var(--text); max-width: 60ch; }
.fc-fail { margin: 0; font-size: 12.5px; color: var(--faint); }
.fc-writing { display: flex; flex-direction: column; gap: 7px; }
.fc-wlabel { display: inline-flex; align-items: center; gap: 7px; font-size: 12px; color: var(--faint); }
.wdot { width: 6px; height: 6px; border-radius: 50%; background: var(--faint); animation: breathe 1.1s ease-in-out infinite; }
@keyframes breathe { 50% { opacity: .25; } }
.fc-skel { height: 8px; border-radius: 4px; background: linear-gradient(90deg, var(--hover, color-mix(in srgb, var(--text) 5%, transparent)) 25%, color-mix(in srgb, var(--text) 9%, transparent) 50%, var(--hover, color-mix(in srgb, var(--text) 5%, transparent)) 75%); background-size: 200% 100%; animation: shimmer 1.3s linear infinite; }
@keyframes shimmer { to { background-position: -200% 0; } }
.fc-acts { display: flex; align-items: center; gap: 8px; margin-top: 11px; flex-wrap: wrap; }
.fc-meta { margin-left: auto; font-size: 11.5px; color: var(--faint); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.gc-line { font-size: 12.5px; color: var(--dim); margin-bottom: 10px; }
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 26px; padding: 0 10px; border-radius: 7px; font: inherit; font-weight: 540; font-size: 12.5px; background: none; border: 0; color: inherit; cursor: pointer; transition: background .12s, color .12s, filter .12s; white-space: nowrap; }
.btn.ghost { color: var(--dim); border: 1px solid var(--border); background: var(--surface); }
.btn.ghost:hover { background: var(--surface-2); color: var(--text); }
.btn.primary { background: var(--lime); color: var(--ink); font-weight: 620; }
.btn.primary:hover { filter: brightness(1.05); }
.btn.link { height: 26px; padding: 0 2px; color: var(--accent); font-weight: 560; }
.btn.link:hover { text-decoration: underline; }
.fdivider { display: flex; align-items: center; gap: 10px; margin: 14px 12px 6px; font-size: 11px; font-weight: 560; color: var(--faint); letter-spacing: .02em; }
.fdivider::after { content: ''; flex: 1; height: 1px; background: var(--border); }
.ffoot { margin-top: 14px; border-top: 1px solid var(--border); padding-top: 8px; }
.fdone { display: inline-flex; align-items: center; gap: 7px; height: 30px; padding: 0 10px 0 12px; border-radius: 8px; font: inherit; font-size: 12.5px; color: var(--dim); background: none; border: 0; cursor: pointer; }
.fdone:hover { background: var(--hover, color-mix(in srgb, var(--text) 5%, transparent)); color: var(--text); }
.done-list { margin-top: 4px; }
.done-list .ftitle { color: var(--faint); }
.done-list .frow-main { height: 36px; }
</style>

<style>
/* The picker is teleported to body, so these rules cannot be scoped. */
.fpop-catcher { position: fixed; inset: 0; z-index: 59; }
.fpop { position: fixed; z-index: 60; width: 270px; padding: 6px; border-radius: 12px; background: var(--surface); box-shadow: var(--shadow-pop); animation: fpop-in .13s cubic-bezier(.2,.8,.2,1); }
@keyframes fpop-in { from { opacity: 0; transform: translateY(-4px) scale(.98); } }
.fpop-in { width: 100%; height: 32px; border-radius: 8px; border: 1px solid var(--border); background: var(--surface); color: var(--text); font: inherit; font-size: 13px; padding: 0 10px; outline: none; transition: border-color .12s, box-shadow .12s; }
.fpop-in:focus { border-color: var(--lime); box-shadow: 0 0 0 3px color-mix(in srgb, var(--lime) 18%, transparent); }
.fpop-list { list-style: none; margin: 6px 0 0; padding: 0; max-height: 300px; overflow: auto; scrollbar-width: thin; }
.fpop-opt { display: flex; align-items: center; gap: 8px; height: 30px; padding: 0 8px 0 6px; border-radius: 7px; font-size: 13px; cursor: pointer; }
.fpop-opt.active { background: var(--hover, color-mix(in srgb, var(--text) 5%, transparent)); }
.fpop-check { width: 14px; flex: none; color: var(--lime); display: inline-flex; }
.fpop-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fpop-n { flex: none; font-size: 11.5px; color: var(--faint); font-variant-numeric: tabular-nums; }
.fpop-none { padding: 10px 8px; font-size: 12.5px; color: var(--faint); }
</style>
