<script setup lang="ts">
// The top bar's notch: the live `running` frame as a Dynamic-Island pill
// (dashboard/proto-workspace/run-canvas/app.js:1201-1270). It reads only the
// feed — no fetch, no stream, no hash — and emits a row click for the shell to
// open the Run's tab and link.
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import type { LiveRunning } from '../lib/types'
import { CLOCK_MS } from '../lib/liveness'
import {
  childTitle,
  compactParts,
  ctxFill,
  fmtElapsed,
  notchView,
  phaseLine,
  rowTitle,
  stageOf,
} from '../lib/notch'

const props = defineProps<{ frame: LiveRunning | null; room: number }>()
const emit = defineEmits<{ 'open-run': [project: string, runId: string] }>()

const view = computed(() => notchView(props.frame))
const parts = computed(() => compactParts(props.room))

// ── Open / close with the prototype's hover timings and click-to-pin ─────────
const open = ref(false)
let pinned = false
let tIn: ReturnType<typeof setTimeout> | null = null
let tOut: ReturnType<typeof setTimeout> | null = null

function clearTimers() {
  if (tIn) clearTimeout(tIn)
  if (tOut) clearTimeout(tOut)
  tIn = null
  tOut = null
}
function enter() {
  if (tOut) clearTimeout(tOut)
  tOut = null
  if (!open.value) tIn = setTimeout(() => (open.value = true), 110)
}
function leave() {
  if (tIn) clearTimeout(tIn)
  tIn = null
  if (!pinned) tOut = setTimeout(() => (open.value = false), 200)
}
function toggle() {
  clearTimers()
  if (open.value && pinned) {
    open.value = false
    pinned = false
  } else {
    open.value = true
    pinned = true
  }
}
function go(r: { project: string; run_id: string }) {
  clearTimers()
  open.value = false
  pinned = false
  emit('open-run', r.project, r.run_id)
}

/** The shell's top-of-stack Escape: close the notch if it is open. */
function escape(): boolean {
  if (!open.value) return false
  clearTimers()
  open.value = false
  pinned = false
  return true
}
defineExpose({ escape })

// ── Outside click closes ─────────────────────────────────────────────────────
const wrap = ref<HTMLElement | null>(null)
function outside(e: MouseEvent) {
  if (open.value && wrap.value && !wrap.value.contains(e.target as Node)) {
    clearTimers()
    open.value = false
    pinned = false
  }
}

// ── Display clock: the elapsed label ticks; it fetches nothing ───────────────
const nowMs = ref(Date.now())

// ── Measured size: the pill morphs between the compact and expanded natural sizes ──
const cEl = ref<HTMLElement | null>(null)
const lEl = ref<HTMLElement | null>(null)
const nat = reactive({ cw: 0, ch: 0, lw: 0, lh: 0 })
let ro: ResizeObserver | null = null
function measure() {
  if (cEl.value) {
    nat.cw = cEl.value.offsetWidth
    nat.ch = cEl.value.offsetHeight
  }
  if (lEl.value) {
    nat.lw = lEl.value.offsetWidth
    nat.lh = lEl.value.offsetHeight
  }
}
const size = computed(() =>
  open.value
    ? { w: Math.max(nat.lw, nat.cw), h: nat.lh }
    : { w: nat.cw, h: nat.ch },
)

onMounted(() => {
  measure()
  ro = new ResizeObserver(measure)
  if (cEl.value) ro.observe(cEl.value)
  if (lEl.value) ro.observe(lEl.value)
  document.addEventListener('mousedown', outside, true)
  setInterval(() => (nowMs.value = Date.now()), CLOCK_MS)
})

onBeforeUnmount(() => {
  ro?.disconnect()
  document.removeEventListener('mousedown', outside, true)
  clearTimers()
})
</script>

<template>
  <div ref="wrap" class="notch-wrap" :style="{ '--notch-max': room + 'px' }">
    <div
      class="notch"
      tabindex="0"
      role="button"
      :aria-expanded="open ? 'true' : 'false'"
      :aria-label="view.rows.length ? view.rows.length + ' Runs running' : 'Nothing running'"
      :class="{ open, idle: !view.first }"
      :style="{ width: size.w + 'px', height: size.h + 'px' }"
      @mouseenter="enter"
      @mouseleave="leave"
      @click="toggle"
      @keydown.enter.prevent="toggle"
    >
      <div ref="cEl" class="nc" :class="{ hide: open, 'is-idle': !view.first }">
        <template v-if="view.first">
          <span class="pulse"></span>
          <span v-if="parts.project" class="nc-proj">{{ view.first.project }}</span>
          <span class="nc-title"><b>{{ rowTitle(view.first).num }}</b> {{ rowTitle(view.first).text }}</span>
          <span class="ndiv"></span>
          <span class="nc-phase">{{ phaseLine(view.first, parts.model) }}</span>
          <span class="nc-time">{{ fmtElapsed(view.first.attempt_started_at, nowMs) }}</span>
          <span class="tbar"><i :class="{ hot: ctxFill(view.first.context).hot }" :style="{ width: ctxFill(view.first.context).pct + '%' }"></i></span>
          <span v-if="view.more > 0" class="nc-more">+{{ view.more }}</span>
        </template>
        <span v-else class="idle-line"></span>
      </div>

      <div
        ref="lEl"
        class="nl"
        :class="{ hide: !open }"
        :style="{ minWidth: view.groups.length ? Math.max(440, nat.cw) + 'px' : '' }"
      >
        <template v-if="view.groups.length">
          <div class="nl-head">{{ view.rows.length }} running<span>context to warn</span></div>
          <template v-for="g in view.groups" :key="g.key">
            <div v-if="g.header" class="nl-batch">{{ g.header }}</div>
            <template v-for="it in g.items" :key="it.key">
              <button
                v-if="it.type === 'run'"
                class="nl-row"
                :tabindex="open ? 0 : -1"
                @click.stop="go(it.run)"
              >
                <span class="pulse"></span>
                <span class="nl-main">
                  <span class="nl-l1"><span class="nl-proj">{{ it.run.project }}</span><b>{{ rowTitle(it.run).num }}</b> {{ rowTitle(it.run).text }}</span>
                  <span class="nl-l2"><span v-if="stageOf(it.run)" class="nl-stage">{{ stageOf(it.run) }}</span><template v-if="stageOf(it.run)"> · </template>{{ phaseLine(it.run) }}</span>
                </span>
                <span class="nl-side">
                  <span class="nc-time">{{ fmtElapsed(it.run.attempt_started_at, nowMs) }}</span>
                  <span class="tbar"><i :class="{ hot: ctxFill(it.run.context).hot }" :style="{ width: ctxFill(it.run.context).pct + '%' }"></i></span>
                </span>
              </button>
              <div v-else class="nl-row nl-wait" @click.stop>
                <span class="nl-dot"></span>
                <span class="nl-main">
                  <span class="nl-l1"><span class="nl-proj">{{ it.child.project }}</span><b>{{ childTitle(it.child).num }}</b> {{ childTitle(it.child).text }}</span>
                </span>
                <span v-if="it.word" class="nl-word">{{ it.word }}</span>
              </div>
            </template>
          </template>
        </template>
        <div v-else class="nl-empty">Nothing running</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* notch (Dynamic Island) — dashboard/proto-workspace/run-canvas/styles.css:598-646 */
.notch-wrap {
  position: absolute;
  top: 0;
  left: 50%;
  transform: translateX(-50%);
  z-index: 1;
  display: flex;
  justify-content: center;
}
.notch {
  --n-bg: #0c0d0e;
  --n-text: #f4f5f1;
  --n-muted: #9a9d95;
  --n-faint: #6c6f68;
  --n-line: rgba(255, 255, 255, 0.08);
  position: relative;
  overflow: hidden;
  background: var(--n-bg);
  color: var(--n-text);
  cursor: pointer;
  outline: none;
  border-radius: 0 0 17px 17px;
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.14), 0 1px 2px rgba(0, 0, 0, 0.18);
  transition:
    width 360ms var(--ease-pane),
    height 360ms var(--ease-pane),
    border-radius 360ms var(--ease-pane),
    box-shadow 0.2s;
}
:global([data-theme='dark']) .notch {
  --n-bg: #000;
  box-shadow: 0 0 0 0.5px rgba(255, 255, 255, 0.09), 0 10px 30px rgba(0, 0, 0, 0.5);
}
@media (prefers-color-scheme: dark) {
  :global(:root:not([data-theme='light'])) .notch {
    --n-bg: #000;
    box-shadow: 0 0 0 0.5px rgba(255, 255, 255, 0.09), 0 10px 30px rgba(0, 0, 0, 0.5);
  }
}
.notch.open {
  border-radius: 0 0 22px 22px;
  box-shadow: 0 18px 50px rgba(0, 0, 0, 0.28), 0 2px 6px rgba(0, 0, 0, 0.2);
  cursor: default;
}
.notch.idle:not(.open) {
  border-radius: 0 0 9px 9px;
  box-shadow: none;
}
.notch:focus-visible {
  box-shadow: 0 0 0 2px var(--purple);
}
.nc,
.nl {
  position: absolute;
  top: 0;
  left: 50%;
  transform: translateX(-50%);
  width: max-content;
  transition: opacity 200ms ease-out 90ms, filter 200ms ease-out 90ms, transform 360ms var(--ease-pane);
}
.nc.hide,
.nl.hide {
  opacity: 0;
  filter: blur(3px);
  pointer-events: none;
  transition-delay: 0s;
  transition-duration: 120ms, 120ms, 360ms;
}
.nl.hide {
  transform: translateX(-50%) scale(0.96);
  transform-origin: top center;
}
.nc.hide {
  transform: translateX(-50%) scale(1.04);
}
.nc {
  display: flex;
  align-items: center;
  gap: 9px;
  height: 32px;
  padding: 0 14px 0 13px;
  font-size: 12.5px;
  white-space: nowrap;
  max-width: var(--notch-max, 640px);
}
.nc > * {
  flex: none;
}
.nc > .nc-title {
  flex: 0 1 auto;
  min-width: 70px;
}
.nc > .nc-phase {
  flex: none;
}
.nc-proj {
  color: var(--n-muted);
}
.nc-title {
  max-width: 230px;
  overflow: hidden;
  text-overflow: ellipsis;
  font-weight: 540;
}
.nc-title b,
.nl-l1 b {
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  margin-right: 1px;
}
.nc-phase {
  color: var(--n-muted);
}
.nc-time {
  font-variant-numeric: tabular-nums;
  color: var(--n-text);
  min-width: 4ch;
  text-align: right;
}
.nc-more {
  height: 18px;
  padding: 0 6px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.1);
  color: var(--n-text);
  font-size: 11px;
  font-weight: 650;
  display: inline-flex;
  align-items: center;
}
.ndiv {
  width: 1px;
  height: 12px;
  background: var(--n-line);
}
.pulse {
  position: relative;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--lime);
  flex: none;
}
.pulse::after {
  content: '';
  position: absolute;
  inset: -3px;
  border-radius: 50%;
  border: 1.5px solid var(--lime);
  opacity: 0;
  animation: ring 1.8s ease-out infinite;
}
@keyframes ring {
  0% {
    transform: scale(0.5);
    opacity: 0.7;
  }
  80%,
  100% {
    transform: scale(1.5);
    opacity: 0;
  }
}
.tbar {
  width: 34px;
  height: 4px;
  border-radius: 2px;
  background: rgba(255, 255, 255, 0.14);
  overflow: hidden;
  flex: none;
}
.tbar i {
  display: block;
  height: 100%;
  border-radius: 2px;
  background: var(--lime);
}
.tbar i.hot {
  background: #fbbf24;
}
.idle-line {
  display: block;
  width: 26px;
  height: 3px;
  border-radius: 2px;
  background: rgba(255, 255, 255, 0.16);
  margin: 0 auto;
}
.nc.is-idle {
  height: 12px;
  padding: 0 20px;
  justify-content: center;
}
.nl {
  padding: 8px 8px 9px;
}
.nl-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 10px 7px;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--n-faint);
  letter-spacing: 0.02em;
}
.nl-head span {
  margin-left: auto;
  font-weight: 500;
}
.nl-row {
  display: flex;
  align-items: center;
  gap: 11px;
  width: 100%;
  padding: 9px 10px;
  border-radius: 12px;
  text-align: left;
  color: var(--n-text);
  transition: background 0.12s;
}
.nl-row:hover,
.nl-row:focus-visible {
  background: rgba(255, 255, 255, 0.07);
  outline: none;
}
.nl-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.nl-l1 {
  font-size: 13px;
  font-weight: 540;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 380px;
}
.nl-proj {
  color: var(--n-muted);
  font-weight: 500;
  margin-right: 7px;
}
.nl-l2 {
  font-size: 12px;
  color: var(--n-muted);
  white-space: nowrap;
}
.nl-stage {
  color: var(--n-faint);
}
.nl-side {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 7px;
  flex: none;
  font-size: 12.5px;
}
.nl-batch {
  padding: 8px 10px 3px;
  font-size: 11px;
  color: var(--n-faint);
}
.nl-wait {
  opacity: 0.5;
  cursor: default;
}
.nl-wait:hover {
  background: none;
}
.nl-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--n-faint);
  flex: none;
}
.nl-word {
  flex: none;
  font-size: 12px;
  color: var(--n-muted);
}
.nl-empty {
  padding: 4px 12px 6px;
  font-size: 12.5px;
  color: var(--n-muted);
  white-space: nowrap;
}
</style>
