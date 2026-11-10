<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from "vue";
import type { TrayModel } from "../lib/modelTray";
import { filterTray } from "../lib/modelTray";
import { money } from "../lib/profilesPage";
import { modelColor, modelLogo, modelName } from "../lib/models";

const props = defineProps<{
  models: TrayModel[];
  notionalOn: boolean;
  current?: string | null;
  autofocus?: boolean;
}>();

const emit = defineEmits<{ pick: [id: string] }>();

// the search is local to the picker — it never touches the tray's query
const q = ref("");
const qEl = ref<HTMLInputElement | null>(null);
const CAP = 60;

const rows = computed(() => filterTray(props.models, q.value));
const shown = computed(() => rows.value.slice(0, CAP));
const rest = computed(() => rows.value.length - shown.value.length);

/** one meta figure: `sub` when the leg rides the subscription, else the $ figure with its session count, else the provider */
function meta(c: TrayModel): string {
  if (c.cc && !props.notionalOn) return "sub";
  if (c.runs > 0) return money(c.avg);
  return c.provider;
}

function onEnter() {
  const first = shown.value[0];
  if (first) emit("pick", first.id);
}

onMounted(() => {
  if (props.autofocus) void nextTick(() => qEl.value?.focus());
});
</script>

<template>
  <input
    ref="qEl"
    v-model="q"
    class="pp-search"
    placeholder="filter models…"
    autocomplete="off"
    spellcheck="false"
    aria-label="filter models"
    @keydown.enter.prevent="onEnter"
  />
  <div class="mlist">
    <button
      v-for="c in shown"
      :key="c.id"
      class="mrow"
      :class="{ on: c.id === current }"
      :title="`${c.note} · ${c.id}`"
      @click="emit('pick', c.id)"
    >
      <span class="role-logo" :style="{ color: modelColor(c.id) }" v-html="modelLogo(c.id)" />
      <span class="mrow-name">
        <b>{{ modelName(c.id) }}<em v-if="c.cc" class="cc-tag">cc</em></b>
        <small class="mono">{{ c.id }}</small>
      </span>
      <span class="mrow-meta mono">
        {{ meta(c) }}<small v-if="c.runs > 0 && !(c.cc && !notionalOn)">{{ c.runs }} sessions</small>
      </span>
    </button>
    <div v-if="rest > 0" class="mrow-none faint">… {{ rest }} more — keep typing</div>
    <div v-else-if="!rows.length" class="mrow-none faint">no model matches</div>
  </div>
</template>

<style scoped>
.pp-search {
  width: 100%;
  padding: 7px 10px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--text);
  font-family: var(--mono);
  font-size: 12px;
  outline: none;
}

.pp-search:focus {
  border-color: var(--purple);
}

.mlist {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border);
  border-radius: 10px;
  max-height: 268px;
  overflow-y: auto;
}

.mrow {
  display: grid;
  grid-template-columns: 26px minmax(0, 1fr) auto;
  gap: 9px;
  align-items: center;
  width: 100%;
  text-align: left;
  padding: 7px 10px;
  border-bottom: 1px solid var(--border);
}

.mrow:last-child {
  border-bottom: none;
}

.mrow:hover,
.mrow.on {
  background: var(--surface-2);
}

.mrow-name {
  min-width: 0;
}

.mrow-name b {
  display: block;
  font-size: 12.5px;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.mrow-name small {
  display: block;
  font-family: var(--mono);
  font-size: 10px;
  color: var(--faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.cc-tag {
  font-style: normal;
  font-family: var(--mono);
  font-size: 9px;
  border: 1px solid var(--border-strong);
  border-radius: 4px;
  padding: 0 4px;
  margin-left: 4px;
  color: var(--lime-ink);
}

.mrow-meta {
  font-size: 11.5px;
  text-align: right;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.mrow-meta small {
  display: block;
  font-size: 9.5px;
  color: var(--faint);
}

.mrow-none {
  padding: 14px;
  font-size: 12px;
}
</style>
