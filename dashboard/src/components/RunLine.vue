<script setup lang="ts">
// The Run line for the terminal's top bar. All of its text lives in
// `lib/runLine.ts`; this file only wires the props, the 1 s clock and the
// open-tab emit. It is mounted by 267.

import { computed, onMounted, onUnmounted, ref } from "vue";
import type { FeedEvent, RunDetail } from "../lib/types";
import { feedRows, runLine } from "../lib/runLine";

const props = defineProps<{
  run: RunDetail | null;
  events: FeedEvent[];
}>();

const emit = defineEmits<{ openRunTab: [] }>();

const nowMs = ref(Date.now());

// Kept apart from `line` so the 1 s tick does not re-serialize every event.
const rows = computed(() =>
  props.run ? feedRows(props.run.run.run_id, props.events) : [],
);

const line = computed(() =>
  props.run ? runLine(props.run, rows.value, nowMs.value) : null,
);

let tick: ReturnType<typeof setInterval> | null = null;
onMounted(() => {
  tick = setInterval(() => (nowMs.value = Date.now()), 1000);
});
onUnmounted(() => {
  if (tick !== null) clearInterval(tick);
});
</script>

<template>
  <!-- PLACEMENT row 12: nothing but the line here. -->
  <button
    v-if="line"
    type="button"
    class="run-line"
    :class="line.tint"
    :aria-label="line.text + ' — open the Run tab'"
    @click="emit('openRunTab')"
  >
    <span class="rl-dot" :class="line.dot">●</span>
    <span class="rl-text">{{ line.label }}</span>
  </button>
</template>

<style scoped>
.run-line {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 8px;
  border: 0;
  border-radius: 6px;
  background: none;
  /* The bar's colour; the terminal is dark. */
  color: inherit;
  font: 12px var(--mono);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  cursor: pointer;
}

.run-line:hover {
  background: color-mix(in srgb, var(--text) 7%, transparent);
}

.run-line:focus-visible {
  outline: none;
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--lime) 40%, transparent);
}

.run-line.bad {
  color: var(--bad);
}

.run-line.warn {
  color: var(--warn);
}

.rl-dot.run {
  color: var(--run);
}

.rl-dot.ok {
  color: var(--ok);
}

.rl-dot.bad {
  color: var(--bad);
}

.rl-dot.warn {
  color: var(--warn);
}

.rl-dot.neutral {
  color: var(--faint);
}
</style>
