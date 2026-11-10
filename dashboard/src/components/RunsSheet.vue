<script setup lang="ts">
// The Runs slide-over (spec fixed item 7): a status filter, day groups and rows
// that open the Run detail tab. It owns no Escape binding — the shell's one
// Escape reaches it through Workspace.closeTop; an outside click closes here.
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import type { ProviderSpend, RunSummary } from '../lib/types'
import { fetchSpend } from '../lib/api'
import { CLOCK_MS } from '../lib/liveness'
import { spendLine } from '../lib/cash'
import { fmtCost } from '../lib/format'
import { glyph } from '../lib/fileIcons'
import { RUN_FILTERS, filterCount, type RunFilter } from '../lib/runsSheet'
import RunsList from './RunsList.vue'

const props = defineProps<{
  runs: RunSummary[]
  loaded: boolean
  apiError: string | null
}>()
const emit = defineEmits<{ close: []; open: [project: string, runId: string] }>()

const filter = ref<RunFilter>('all')

// The label clock (elapsed, idle, age) ticks while the sheet is open; the pool
// itself refetches on the live feed's `running` (App.vue).
const nowMs = ref(Date.now())
let clockTimer: ReturnType<typeof setInterval> | undefined

// 215's sidebar spend line, fetched once per mount; a failed fetch just leaves
// the line out (`split.sub` false, `split.cash` 0) and the sheet stays usable.
const spend = shallowRef<ProviderSpend[]>([])
const split = computed(() => spendLine(spend.value))

onMounted(() => {
  fetchSpend()
    .then((s) => (spend.value = s))
    .catch(() => {})
  clockTimer = setInterval(() => (nowMs.value = Date.now()), CLOCK_MS)
})

onUnmounted(() => clearInterval(clockTimer))
</script>

<template>
  <div class="rs-layer">
    <div class="rs-scrim" @mousedown="emit('close')" />
    <div class="rs-sheet" role="dialog" aria-label="Runs">
      <div class="rs-head">
        <div class="rs-title">Runs</div>
        <div class="rs-seg" role="tablist">
          <button
            v-for="f in RUN_FILTERS"
            :key="f.id"
            :class="{ on: filter === f.id }"
            @click="filter = f.id"
          >
            {{ f.label }}<span class="seg-n">{{ filterCount(runs, f.id) }}</span>
          </button>
        </div>
        <button
          class="rs-close"
          title="Close  Esc"
          @click="emit('close')"
          v-html="glyph('x', 16)"
        />
      </div>

      <div v-if="spend.length" class="rs-spend">
        Spend · 7 days <b>cash {{ fmtCost(split.cash) }}</b
        ><span v-if="split.sub" class="faint rs-sub" :title="split.subTitle"> +sub</span>
      </div>
      <div v-if="apiError" class="rs-api">api unreachable — retrying {{ apiError }}</div>

      <div class="rs-body">
        <RunsList
          :runs="runs"
          :loaded="loaded"
          :now-ms="nowMs"
          :filter="filter"
          @open="(p, r) => emit('open', p, r)"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.rs-layer {
  display: contents;
}
.rs-scrim {
  position: fixed;
  inset: 0;
  z-index: 40;
  background: color-mix(in srgb, var(--text) 18%, transparent);
}
.rs-sheet {
  position: fixed;
  top: 52px;
  right: 8px;
  bottom: 8px;
  width: min(660px, calc(100vw - 40px));
  z-index: 50;
  background: var(--panel);
  border-radius: 14px;
  box-shadow: var(--shadow-pop);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.rs-head {
  flex: none;
  display: flex;
  align-items: center;
  gap: 14px;
  height: 56px;
  padding: 0 12px 0 20px;
  border-bottom: 1px solid var(--line);
}
.rs-title {
  font-size: 15px;
  font-weight: 640;
  letter-spacing: -0.01em;
  margin-right: auto;
}
.rs-seg {
  display: inline-flex;
  padding: 2px;
  gap: 1px;
  border-radius: 9px;
  background: var(--panel-2);
}
.rs-seg > button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 10px;
  border-radius: 7px;
  font: inherit;
  font-size: 12.5px;
  font-weight: 500;
  color: var(--muted);
  transition: color 0.12s, background 0.16s;
}
.rs-seg > button:hover {
  color: var(--text);
}
.rs-seg > button.on {
  color: var(--text);
  background: var(--panel);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08), 0 0 0 0.5px var(--line);
}
.seg-n {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--faint);
}
.rs-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 8px;
  color: var(--muted);
  transition: background 0.12s, color 0.12s;
}
.rs-close:hover {
  background: var(--hover);
  color: var(--text);
}
.rs-spend {
  flex: none;
  padding: 10px 20px 4px;
  font-size: 12px;
  color: var(--faint);
}
.rs-spend b {
  color: var(--muted);
  font-weight: 560;
}
.rs-sub {
  cursor: help;
}
.rs-api {
  flex: none;
  padding: 6px 20px;
  font-size: 12px;
  color: var(--bad);
}
.rs-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--line) transparent;
}

/* Slide-over: the sheet sweeps in from the right, the scrim fades. */
.rs-enter-active .rs-sheet,
.rs-leave-active .rs-sheet {
  transition: transform 190ms cubic-bezier(0.32, 0.72, 0, 1), opacity 190ms ease;
}
.rs-enter-from .rs-sheet,
.rs-leave-to .rs-sheet {
  transform: translateX(24px);
  opacity: 0;
}
.rs-enter-active .rs-scrim,
.rs-leave-active .rs-scrim {
  transition: opacity 190ms ease;
}
.rs-enter-from .rs-scrim,
.rs-leave-to .rs-scrim {
  opacity: 0;
}
</style>
