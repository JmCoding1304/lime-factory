<script setup lang="ts">
// The dock's [Run] tab body. All of its data lives in `lib/runLine.ts`; this
// file only wires the props, the 1 s clock and the open-phase emit. It is
// mounted by 267.

import { computed, onMounted, onUnmounted, ref } from "vue";
import type { FeedEvent, RunDetail } from "../lib/types";
import { feedRows, runInfo } from "../lib/runLine";
import StatusChip from "./StatusChip.vue";

const props = defineProps<{
  run: RunDetail | null;
  events: FeedEvent[];
}>();

const emit = defineEmits<{ openPhase: [phaseId: string] }>();

const nowMs = ref(Date.now());

// Kept apart from `info` so the 1 s tick does not re-serialize every event.
const rows = computed(() =>
  props.run ? feedRows(props.run.run.run_id, props.events) : [],
);

const info = computed(() =>
  props.run ? runInfo(props.run, rows.value, nowMs.value) : null,
);

// A function, so vue-tsc narrowing holds across the template's v-if branches.
function openFailure() {
  const id = info.value?.failure?.phaseId;
  if (id) emit("openPhase", id);
}

/** Row 16: the rescope notice's "Open <phase>" button. */
function openNotice() {
  const id = info.value?.noticePhase?.id;
  if (id) emit("openPhase", id);
}

let tick: ReturnType<typeof setInterval> | null = null;
onMounted(() => {
  tick = setInterval(() => (nowMs.value = Date.now()), 1000);
});
onUnmounted(() => {
  if (tick !== null) clearInterval(tick);
});
</script>

<template>
  <section v-if="info" class="run-info">
    <div class="ri-head">
      <span class="ri-id">{{ info.id }}</span>
      <StatusChip :status="info.status" />
    </div>

    <div class="ri-title" :title="info.request">{{ info.title }}</div>

    <div v-if="info.notice" class="ri-scope">
      <span class="ri-scope-label">{{ info.notice.headline }}</span>
      <span class="ri-scope-text">
        <span v-for="(line, i) in info.notice.lines" :key="i">{{ line }}</span>
      </span>
      <button
        v-if="info.noticePhase"
        type="button"
        class="ri-open"
        @click="openNotice"
      >
        Open {{ info.noticePhase.name }}
      </button>
    </div>

    <button
      v-else-if="info.failure && info.failure.phaseId"
      type="button"
      class="ri-error"
      @click="openFailure"
    >
      {{ info.failure.text }}
    </button>

    <div v-else-if="info.failure" class="ri-error">
      {{ info.failure.text }}
    </div>

    <div v-if="info.signals.length" class="ri-chips">
      <span
        v-for="s in info.signals"
        :key="s.type"
        class="chip signal"
        :class="s.tone"
        :title="s.title"
        >{{ s.label }}</span
      >
    </div>

    <div class="ri-chips">
      <span
        v-for="f in info.facts"
        :key="f.key"
        class="chip"
        :class="f.key"
        :title="f.title"
        >{{ f.text }}</span
      >
    </div>
  </section>
</template>

<style scoped>
.run-info {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 14px;
  min-width: 0;
}

.ri-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.ri-id {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--dim);
}

.ri-title {
  font-weight: 500;
  overflow-wrap: anywhere;
}

.ri-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
}

.chip {
  /* Run chips are scoped and uncapped; a long branch or worktree wraps. */
  white-space: normal;
  overflow-wrap: anywhere;
  max-width:none;
}

.chip.warns {
  color: var(--warn);
  background: var(--warn-chip);
}

.chip.signal.warn {
  color: var(--warn);
  background: var(--warn-chip);
}

.chip.signal.bad {
  color: var(--bad);
  background: var(--bad-chip);
  border-color: var(--bad);
}

.ri-scope {
  padding: 9px 13px;
  border: 1px solid var(--warn);
  background: var(--warn-chip);
  border-radius: 8px;
  font-size: 12.5px;
  color: var(--warn);
  display: flex;
  gap: 10px;
  align-items: baseline;
}

.ri-scope-label {
  font-weight: 700;
  flex: none;
  font-size: 10.5px;
  letter-spacing: 0.04em;
}

.ri-scope-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-family: var(--mono);
  overflow-wrap: anywhere;
  color: var(--text);
}

.ri-open {
  flex: none;
  align-self: center;
  padding: 3px 9px;
  border: 1px solid var(--warn);
  border-radius: 6px;
  background: none;
  color: var(--warn);
  font-family: var(--mono);
  font-size: 11.5px;
  cursor: pointer;
}

.ri-open:hover {
  background: var(--warn-chip);
}

.ri-error {
  padding: 9px 13px;
  border: 1px solid var(--bad);
  background: var(--bad-chip);
  border-radius: 8px;
  font-size: 12.5px;
  color: var(--bad);
  font-family: var(--mono);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  text-align: left;
}

button.ri-error {
  cursor: pointer;
  width: 100%;
}
</style>
