<script setup lang="ts">
// The Run page: the canvas over the terminal, the dock beside the terminal, and
// the Run line in the terminal bar (spec "Dock beside the terminal", PLACEMENT).
// It reads one Run from the live feed and owns every URL write for the Run link:
// a deep link resolves to a phase, an unknown one is replaced without it, and
// closing a phase pushes so Back reopens. The dock's tab state lives per Run tab
// in `lib/editorTabs.ts`; the canvases, terminal and dock bodies are their own
// components.

import { computed, nextTick, ref, watch } from "vue";
import type { FeedEvent } from "../lib/types";
import { live } from "../lib/live";
import { namesRun, navigate, replaceRoute, resolveLinkPhase, useRoute } from "../lib/router";
import { cardsOf } from "../lib/canvas";
import {
  closeRunDock,
  dockActive,
  escapeDock,
  showPhaseDock,
  toggleRunDock,
  type RunDock,
} from "../lib/editorTabs";
import RunCanvas from "./RunCanvas.vue";
import Terminal from "./Terminal.vue";
import RunLine from "./RunLine.vue";
import RunInfo from "./RunInfo.vue";
import CardDetail from "./CardDetail.vue";

const props = defineProps<{
  project: string;
  runId: string;
  phase: string | null;
  dock: RunDock;
}>();

// A repo artifact opened from the card detail: the shell owns the file tab.
// `phase` reports this tab's selection back, so the tab keeps it while the link
// names another Run (PLACEMENT row 81).
const emit = defineEmits<{ open: [path: string]; phase: [id: string | null] }>();

function onOpenArtifact(path: string) {
  emit("open", path);
}

// ── Feed ─────────────────────────────────────────────────────────────────────
const feed = computed(() => live.runFeed(props.project, props.runId));
const run = computed(() => feed.value?.run ?? null);
const events = computed<FeedEvent[]>(() => feed.value?.events ?? []);

// A top-level ref, so the template unwraps it.
const apiDown = live.down;

// ── Link ─────────────────────────────────────────────────────────────────────
// `props.phase` is this tab's phase: the link's when the link names this Run,
// else the phase the tab last selected (see `tabPhase` in lib/router.ts).
const route = useRoute();
const linked = computed(() =>
  namesRun(route.value, { project: props.project, runId: props.runId }),
);
const link = computed(() =>
  resolveLinkPhase(props.phase, run.value ? run.value.phases : null),
);
const openId = computed(() => link.value.phaseId);

// An unknown phase segment is replaced (never pushed) by the link without it —
// but only when the link names this Run, so a stale per-tab phase never
// hijacks another Run's link.
watch(
  () => link.value.rewrite && linked.value,
  (r) => {
    if (r) replaceRoute(props.project, props.runId);
  },
  { immediate: true },
);

// Report the shown phase back to the tab, so it survives a switch away.
watch(openId, (id) => emit("phase", id), { immediate: true });

// ── Open / reveal ────────────────────────────────────────────────────────────
const canvasEl = ref<InstanceType<typeof RunCanvas> | null>(null);
const termEl = ref<InstanceType<typeof Terminal> | null>(null);

// A card click only wins over a deep link for the run of that click.
let clicked: string | null = null;

watch(
  openId,
  (id) => {
    if (!id) return;
    const centre = id !== clicked;
    clicked = null;
    showPhaseDock(props.dock);
    // After mount, so the terminal ref exists on a cached-Run first render.
    void nextTick(() => {
      termEl.value?.expand();
      canvasEl.value?.reveal(id, centre);
    });
  },
  { immediate: true },
);

// ── Handlers ─────────────────────────────────────────────────────────────────
function onSelect(id: string | null) {
  if (id) {
    clicked = id;
    showPhaseDock(props.dock);
    navigate(props.project, props.runId, id);
  } else {
    navigate(props.project, props.runId);
  }
}

function openPhase(id: string) {
  showPhaseDock(props.dock);
  navigate(props.project, props.runId, id);
}

// Pushes, so Back reopens the phase.
function closePhase() {
  navigate(props.project, props.runId);
}

function onRunLine() {
  toggleRunDock(props.dock);
  if (dockActive(props.dock, openId.value !== null) === "run") {
    termEl.value?.expand();
  }
}

function escape(): boolean {
  const w = escapeDock(props.dock, openId.value !== null);
  if (w === "phase") closePhase();
  return w !== null;
}

defineExpose({ escape });

// ── Derived ──────────────────────────────────────────────────────────────────
const active = computed(() => dockActive(props.dock, openId.value !== null));
const shown = computed(() => active.value !== null);

const openCard = computed(() =>
  run.value && openId.value
    ? cardsOf(
        run.value.phases,
        run.value.sessions,
        events.value,
        Date.now(),
        run.value.run.engineer,
      ).find((c) => c.id === openId.value) ?? null
    : null,
);
</script>

<template>
  <div class="rv">
    <div class="rv-canvas">
      <div v-if="apiDown" class="rv-api">api unreachable — retrying</div>
      <RunCanvas
        ref="canvasEl"
        :run="run"
        :events="events"
        :selected="openId"
        @select="onSelect"
      >
        <template #empty>
          <RunInfo :run="run" :events="events" @open-phase="openPhase" />
        </template>
      </RunCanvas>
    </div>

    <Terminal
      ref="termEl"
      :events="events"
      :phase-id="openId"
      :queued="openCard?.status === 'queued'"
    >
      <template #bar>
        <span class="rv-line" @dblclick.stop>
          <RunLine :run="run" :events="events" @open-run-tab="onRunLine" />
        </span>
      </template>
      <template #dock>
        <aside v-if="shown" class="rv-dock">
          <div class="rv-tabs" role="tablist">
            <div
              v-if="dock.runOpen"
              class="rv-tab"
              :class="{ on: active === 'run' }"
              role="tab"
              :aria-selected="active === 'run'"
              @click="dock.active = 'run'"
            >
              <span class="rv-dot">●</span>
              <span class="rv-tab-name">Run</span>
              <span class="rv-tab-id">{{ run?.run.ticket ?? runId }}</span>
              <span
                class="rv-x"
                role="button"
                aria-label="Close the Run tab"
                @click.stop="closeRunDock(dock)"
                >✕</span
              >
            </div>
            <div
              v-if="openCard"
              class="rv-tab"
              :class="{ on: active === 'phase' }"
              role="tab"
              :aria-selected="active === 'phase'"
              @click="showPhaseDock(dock)"
            >
              <span class="rv-icon" v-html="openCard.icon" />
              <span class="rv-tab-name">{{ openCard.name }}</span>
              <span class="rv-x" role="button" aria-label="Close the phase tab" @click.stop="closePhase"
                >✕</span
              >
            </div>
          </div>
          <div class="rv-body">
            <RunInfo
              v-if="active === 'run'"
              :run="run"
              :events="events"
              @open-phase="openPhase"
            />
            <CardDetail
              v-else-if="active === 'phase'"
              :run="run"
              :events="events"
              :phase-id="openId"
              @open="onOpenArtifact"
            />
          </div>
        </aside>
      </template>
    </Terminal>
  </div>
</template>

<style scoped>
.rv {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  height: 100%;
}

.rv-canvas {
  flex: 1;
  min-height: 0;
  position: relative;
}

/* Row 15: the API banner sits across the canvas top, above its cards. */
.rv-api {
  position: absolute;
  inset: 0 0 auto 0;
  z-index: 5;
  padding: 6px 12px;
  font-family: var(--mono);
  font-size: 12px;
  color: var(--bad);
  background: var(--bad-chip);
  border-bottom: 1px solid var(--bad);
}

.rv-line {
  display: inline-flex;
  align-items: center;
  min-width: 0;
}

/* The dock stays dark in both themes: override the app tokens locally with the
   dark values from style.css. */
.rv-dock {
  --text: #f9f9f9;
  --dim: #a8ac9f;
  --faint: #70746a;
  --border: #2c2c2c;
  --surface-2: #242424;
  --ok: #a8e00f;
  --ok-chip: #2a3413;
  --run: #b28bff;
  --run-chip: #2d2247;
  --bad: #ff7a63;
  --bad-chip: #421f18;
  --warn: #e8b64a;
  --warn-chip: #3a2f12;
  flex: none;
  width: 380px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
  border-left: 1px solid rgba(255, 255, 255, 0.06);
  background: #111214;
  color: #d5d8d1;
}

.rv-tabs {
  display: flex;
  align-items: stretch;
  height: 34px;
  flex: none;
  border-bottom: 1px solid var(--border);
  min-width: 0;
  overflow: hidden;
}

.rv-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0 10px;
  min-width: 0;
  font-size: 12px;
  color: var(--dim);
  border-top: 2px solid transparent;
  cursor: pointer;
}

.rv-tab.on {
  color: var(--text);
  border-top-color: var(--run);
  background: var(--surface-2);
}

.rv-dot {
  color: var(--run);
  font-size: 9px;
  flex: none;
}

.rv-icon {
  display: inline-flex;
  flex: none;
}

.rv-tab-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rv-tab-id {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rv-x {
  flex: none;
  margin-left: 2px;
  padding: 0 3px;
  border-radius: 4px;
  color: var(--faint);
  font-size: 11px;
}

.rv-x:hover {
  color: var(--text);
  background: var(--surface-2);
}

.rv-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
}
</style>
