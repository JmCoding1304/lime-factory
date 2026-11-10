<script setup lang="ts">
import { computed } from 'vue'
import type { RunSummary } from '../lib/types'
import { fmtAgo, fmtDuration } from '../lib/format'
import { statusWord } from '../lib/status'
import { idleLabel } from '../lib/liveness'
import { dayGroups, runName, type RunFilter } from '../lib/runsSheet'

const props = defineProps<{
  runs: RunSummary[]
  loaded: boolean
  nowMs: number
  filter: RunFilter
}>()
const emit = defineEmits<{ open: [project: string, runId: string] }>()

// Newest first, grouped into local-calendar day buckets by the pure model.
// The row name is computed once per Run, not per render.
const groups = computed(() =>
  dayGroups(props.runs, props.filter, props.nowMs).map((g) => ({
    label: g.label,
    items: g.runs.map((run) => ({ run, name: runName(run) })),
  })),
)
</script>

<template>
  <div class="list-wrap">
    <template v-for="g in groups" :key="g.label">
      <div class="group-head">
        {{ g.label }} <span class="n">{{ g.items.length }}</span>
      </div>
      <button
        v-for="it in g.items"
        :key="`${it.run.project}/${it.run.run_id}`"
        type="button"
        class="runrow"
        :class="`st-${it.run.status}`"
        @click="emit('open', it.run.project, it.run.run_id)"
      >
        <span class="st-dot" role="img" :aria-label="statusWord(it.run.status)" />
        <span class="run-what"
          ><span v-if="it.name.num" class="run-num">{{ it.name.num }}</span
          >{{ it.name.text }}</span
        >
        <span class="run-idle">{{ idleLabel(it.run, nowMs) }}</span>
        <span class="chip repo">{{ it.run.project }}</span>
        <span class="run-dur" :class="{ live: it.run.status === 'running' }">{{
          it.run.started_at ? fmtDuration(it.run.started_at, it.run.ended_at, nowMs) : ''
        }}</span>
        <span class="run-age">{{ fmtAgo(it.run.started_at, nowMs) }}</span>
      </button>
    </template>
    <div v-if="!groups.length" class="empty">
      <template v-if="runs.length">No runs with this status.</template>
      <template v-else-if="loaded">no runs yet — `lime run` a ticket to see it here</template>
      <template v-else>loading runs…</template>
    </div>
  </div>
</template>

<style scoped>
.list-wrap {
  padding: 4px 8px 20px;
}

.group-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 12px 6px;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.09em;
  color: var(--faint);
  font-weight: 600;
}

.group-head .n {
  color: var(--dim);
}

.runrow {
  display: grid;
  /* timer: 72px fits "999h 59m" (14.5px, tabular); longer grows the track, never overlaps the age */
  grid-template-columns: 10px minmax(0, 1fr) auto auto minmax(72px, max-content) 64px;
  align-items: center;
  gap: 12px;
  height: 36px;
  padding: 0 12px;
  width: 100%;
  text-align: left;
  border-radius: 9px;
  background: none;
  border: 0;
  font: inherit;
  cursor: pointer;
  color: var(--text);
  transition: background 0.1s;
}

.runrow:hover {
  background: var(--surface-2);
}

.run-what {
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 13.5px;
}

.run-num {
  color: var(--faint);
  font-variant-numeric: tabular-nums;
  margin-right: 5px;
}

.run-idle {
  color: var(--bad);
  font-size: 12px;
  white-space: nowrap;
}

.runrow .chip.repo {
  justify-self: end;
  max-width: 112px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.run-dur {
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  color: var(--dim);
}

.run-dur.live {
  color: var(--run);
}

.run-age {
  text-align: right;
  color: var(--faint);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.st-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  flex: none;
  background: var(--border-strong);
}

.st-running .st-dot {
  background: var(--run);
  box-shadow: 0 0 0 3px var(--run-chip);
  animation: pulse 1.6s ease-in-out infinite;
}

.st-success .st-dot {
  background: var(--ok);
}

.st-deployed .st-dot {
  background: var(--ok);
  outline: 2px solid var(--ok);
  outline-offset: 2px;
}

.st-fail .st-dot,
.st-budget_exhausted .st-dot {
  background: var(--bad);
  border-radius: 2px;
  transform: rotate(45deg) scale(0.9);
}

/* Preserved and awaiting the engineer — a warning dot, not the failure red. */
.st-needs_rescope .st-dot {
  background: var(--warn);
}

.st-discarded .st-dot {
  background: transparent;
  box-shadow: inset 0 0 0 1.5px var(--faint);
}
</style>
