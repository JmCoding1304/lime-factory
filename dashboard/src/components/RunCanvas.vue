<script setup lang="ts">
// The Run canvas: stage rows of phase cards over a dotted, pannable plane.
// Pure geometry and card data live in `lib/canvas.ts`; this file only wires the
// view, the pointer gestures and the selection emit. Mounted by RunView (267).

import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import type { FeedEvent, FeedRun } from "../lib/types";
import {
  GRID,
  type View,
  fitView,
  panBy,
  wheelToView,
  zoomAt,
} from "../lib/bench";
import {
  CARD_H,
  CARD_W,
  MINI_H,
  MINI_W,
  backbone,
  cardsOf,
  layoutCanvas,
  nextSelection,
  ordWord,
  pillFaces,
  revealView,
  toggleTry,
  tryToOpen,
} from "../lib/canvas";
import { fmtK } from "../lib/format";

const props = defineProps<{
  run: FeedRun | null;
  events: FeedEvent[];
  selected: string | null;
}>();

const emit = defineEmits<{ select: [phaseId: string | null] }>();

const phases = computed(() => props.run?.phases ?? []);
const tree = computed(() =>
  backbone(phases.value, props.events, props.run?.run ?? null),
);
const open = ref<ReadonlySet<number>>(new Set());
const layout = computed(() =>
  layoutCanvas(phases.value, tree.value, open.value),
);

const nowMs = ref(Date.now());
const cards = computed(() =>
  props.run
    ? cardsOf(
        props.run.phases,
        props.run.sessions,
        props.events,
        nowMs.value,
        props.run.run.engineer,
        props.run.envelopes ?? [],
        tree.value,
      )
    : [],
);
const faces = computed(() => pillFaces(tree.value, cards.value));
const spineCards = computed(() =>
  cards.value.filter((c) => {
    const p = layout.value.pos[c.id];
    return !!p && !p.mini;
  }),
);
const miniCards = computed(() =>
  cards.value.filter((c) => {
    const p = layout.value.pos[c.id];
    return !!p && p.mini;
  }),
);

/** A branch card in a try that failed reads as past. */
function tryFailed(id: string): boolean {
  const n = tree.value.tryOf[id];
  if (n === undefined) return false;
  const t = tree.value.tries.find((x) => x.n === n);
  return t ? t.failed : false;
}

// ── view ─────────────────────────────────────────────────────────────────────

const view = reactive<View>({ x: 0, y: 0, k: 1 });
const size = ref({ w: 0, h: 0 });

const canvasEl = ref<HTMLElement | null>(null);
const fitted = ref(false);
let lastRunId: string | null = null;

/** Fit the drawn content into the viewport with a clear margin on every side. */
function fit() {
  if (size.value.w > 0 && layout.value.width > 0) {
    Object.assign(
      view,
      fitView(layout.value.bounds, size.value.w, size.value.h, 24),
    );
  }
}

// A reveal asked for before the first fit waits for it.
let pending: { id: string; centre: boolean } | null = null;

/** Pan the view so the card sits clear of the dock, or centre it. */
function reveal(id: string, centre = false): void {
  const pos = layout.value.pos[id];
  if (!pos || !fitted.value || size.value.w <= 0) {
    pending = { id, centre };
    return;
  }
  Object.assign(
    view,
    revealView(view, pos, size.value.w, size.value.h, centre),
  );
}

// Fit once per Run: the first time a size and at least one phase exist, and
// again whenever the Run changes. Later feed frames never move the view; a
// reveal pending on a just-opened column resolves once its card has a position.
watch(
  () => [props.run?.run.run_id ?? null, layout.value, size.value] as const,
  ([runId, , s]) => {
    if (runId !== lastRunId) {
      lastRunId = runId;
      fitted.value = false;
      open.value = new Set();
    }
    if (!fitted.value && layout.value.width > 0 && s.w > 0) {
      fit();
      fitted.value = true;
    }
    if (fitted.value && pending && layout.value.pos[pending.id]) {
      const r = pending;
      pending = null;
      reveal(r.id, r.centre);
    }
  },
  { immediate: true },
);

// A selected (or deep-linked) branch run opens its try once it has a branch.
watch(
  () => props.selected,
  (id) => {
    const n = tryToOpen(id, tree.value);
    if (n !== null && !open.value.has(n)) {
      open.value = new Set([...open.value, n]);
    }
  },
  { immediate: true },
);

// When the terminal grows, the open card stays clear.
watch(size, () => {
  if (fitted.value && props.selected) reveal(props.selected, false);
});

// ── pan / zoom / gestures ────────────────────────────────────────────────────

const panning = ref(false);
let dragMoved = false;

function onPointerDown(e: PointerEvent) {
  if (e.button !== 0) return;
  const target = e.target as Element | null;
  if (target && target.closest(".rc-zoom, .rc-empty, .rc-skel")) return;
  const startX = e.clientX;
  const startY = e.clientY;
  const startView: View = { ...view };
  const move = (ev: PointerEvent) => {
    const dx = ev.clientX - startX;
    const dy = ev.clientY - startY;
    if (!dragMoved && Math.hypot(dx, dy) < 4) return;
    dragMoved = true;
    panning.value = true;
    Object.assign(view, panBy(startView, dx, dy));
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    panning.value = false;
    // swallow the click that follows a drag
    setTimeout(() => (dragMoved = false), 0);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}

function onWheel(e: WheelEvent) {
  const r = canvasEl.value?.getBoundingClientRect();
  if (!r) return;
  Object.assign(
    view,
    wheelToView(view, e, e.clientX - r.left, e.clientY - r.top),
  );
}

interface GestureEvent extends UIEvent {
  scale: number;
}
let startK = 1;

function onGestureStart(e: GestureEvent) {
  startK = view.k;
  e.preventDefault();
}

function onGesture(e: GestureEvent) {
  Object.assign(
    view,
    zoomAt(view, size.value.w / 2, size.value.h / 2, (startK * e.scale) / view.k),
  );
  e.preventDefault();
}

function zoomBy(factor: number) {
  Object.assign(view, zoomAt(view, size.value.w / 2, size.value.h / 2, factor));
}

// ── selection ────────────────────────────────────────────────────────────────

function choose(action: { card: string } | "clear") {
  const next = nextSelection(props.selected, action);
  if (next !== props.selected) emit("select", next);
}

function onCardClick(c: { id: string }) {
  if (!dragMoved) choose({ card: c.id });
}

/** Open or fold a try's column; folding drops a selection inside it. */
function onPillClick(n: number) {
  if (dragMoved) return;
  const r = toggleTry(open.value, n, props.selected, tree.value);
  open.value = r.open;
  if (r.clear) emit("select", null);
}

function onCanvasClick() {
  if (!dragMoved) choose("clear");
}

/** The shell's Escape: clear the selection if any, else decline. */
function escape(): boolean {
  if (props.selected === null) return false;
  emit("select", null);
  return true;
}

defineExpose({ fit, escape, reveal });

// ── lifecycle ────────────────────────────────────────────────────────────────

let timer: ReturnType<typeof setInterval> | undefined;
let ro: ResizeObserver | undefined;

onMounted(() => {
  timer = setInterval(() => (nowMs.value = Date.now()), 1000);
  ro = new ResizeObserver((entries) => {
    const entry = entries[0];
    if (!entry) return;
    const { width, height } = entry.contentRect;
    size.value = { w: width, h: height };
  });
  if (canvasEl.value) ro.observe(canvasEl.value);
});

onUnmounted(() => {
  if (timer) clearInterval(timer);
  ro?.disconnect();
});
</script>

<template>
  <!-- The context bar fills toward the Session's `budget_warn`, never a fixed
       number; see `contextBar` in lib/canvas.ts. -->
  <div
    ref="canvasEl"
    class="rc-canvas"
    :class="{ grabbing: panning }"
    tabindex="0"
    :style="{
      backgroundPosition: `${view.x}px ${view.y}px`,
      backgroundSize: `${GRID * view.k}px ${GRID * view.k}px`,
    }"
    @pointerdown="onPointerDown"
    @click="onCanvasClick"
    @wheel.prevent="onWheel"
    @gesturestart.prevent="onGestureStart"
    @gesturechange.prevent="onGesture"
    @gestureend.prevent
  >
    <div
      v-if="run && run.phases.length"
      class="rc-world"
      :style="{
        transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})`,
        width: layout.width + 'px',
        height: layout.height + 'px',
      }"
    >
      <svg
        class="rc-links"
        :width="layout.width"
        :height="layout.height"
        aria-hidden="true"
      >
        <g
          v-for="e in layout.edges"
          :key="e.id"
          class="cn"
          :class="{ done: e.done, flowing: e.flowing, broken: e.broken, trunk: e.trunk }"
        >
          <path class="e-line" :d="e.d" />
          <path class="e-head" :d="e.head" />
        </g>
      </svg>

      <div
        v-for="l in layout.labels"
        :key="l.text"
        class="rc-stage"
        :style="{ left: l.x + 'px', top: l.y + 'px' }"
      >
        {{ l.text }}
      </div>

      <button
        v-for="c in spineCards"
        :key="c.id"
        class="rc-card"
        :class="['p-' + c.status, { sel: c.id === selected, ended: c.ended }]"
        :data-id="c.id"
        :title="c.name + ' · ' + c.id"
        :aria-pressed="c.id === selected ? 'true' : 'false'"
        :style="{
          left: layout.pos[c.id].x + 'px',
          top: layout.pos[c.id].y + 'px',
          width: CARD_W + 'px',
          height: CARD_H + 'px',
        }"
        @click.stop="onCardClick(c)"
      >
        <span class="c-top">
          <span class="c-icon" v-html="c.icon" />
          <span class="c-names">
            <span class="c-name">{{ c.label }}</span>
            <span class="c-owner" :title="c.model ?? ''">{{ c.ownerLine }}</span>
          </span>
          <span class="c-mark" :class="'m-' + c.mark">
            <template v-if="c.mark === 'success'">✓</template>
            <template v-else-if="c.mark === 'fail' || c.mark === 'blocked'">✕</template>
            <span v-else-if="c.mark === 'running'" class="pulse" />
            <template v-else>○</template>
          </span>
        </span>
        <span class="c-stats">
          <span v-if="c.note" class="c-runs">{{ c.note }}</span>
          <span
            v-else-if="c.ctx"
            class="cbar"
            :title="fmtK(c.ctx.used) + ' / ' + fmtK(c.ctx.warn)"
          >
            <i :style="{ width: c.ctx.pct + '%' }" :class="{ hot: c.ctx.hot }" />
          </span>
          <span class="c-time" :class="[{ live: c.live }, c.tone && 't-' + c.tone]">
            {{ c.time }}
          </span>
        </span>
        <span v-if="c.warn || c.fresh" class="c-badges">
          <span v-if="c.warn" class="nb" title="budget warning">▲</span>
          <span v-if="c.fresh" class="nb" title="fresh session">↻</span>
        </span>
      </button>

      <button
        v-for="p in layout.pills"
        :key="p.key"
        class="rc-pill"
        :class="faces[p.key]?.ok === true ? 'is-ok' : faces[p.key]?.ok === false ? 'is-bad' : ''"
        :style="{ left: p.x + 'px', top: p.y + 'px' }"
        :title="faces[p.key].tip + (p.open ? ' (click to fold)' : ' (click to open)')"
        @click.stop="onPillClick(p.try)"
      >
        <b>{{ faces[p.key].title }}</b>
        <template v-if="faces[p.key].flag">
          <span class="sep">·</span>{{ faces[p.key].flag }}
        </template>
        <span class="sep">·</span>{{ faces[p.key].time }}
        <span class="sep">·</span>{{ faces[p.key].size }}
        <span class="sep">·</span>
        <span
          :class="faces[p.key].ok === true ? 'ok' : faces[p.key].ok === false ? 'bad' : ''"
        >{{ faces[p.key].verdict }}</span>
        <span class="lane-tog">{{ p.open ? '▾' : '▸' }}</span>
      </button>

      <button
        v-for="c in miniCards"
        :key="c.id"
        class="rc-mini"
        :class="{ sel: c.id === selected, ended: c.ended, past: tryFailed(c.id) }"
        :data-id="c.id"
        :title="c.name + ' · ' + c.id"
        :aria-pressed="c.id === selected ? 'true' : 'false'"
        :style="{
          left: layout.pos[c.id].x + 'px',
          top: layout.pos[c.id].y + 'px',
          width: MINI_W + 'px',
          height: MINI_H + 'px',
        }"
        @click.stop="onCardClick(c)"
      >
        <span class="m-top">
          <span class="m-icon" v-html="c.icon" />
          <span class="m-name">{{ c.label }}<span v-if="c.ord > 1" class="c-ord"> · {{ ordWord(c.ord) }}</span></span>
          <span class="c-mark" :class="'m-' + c.mark">
            <template v-if="c.mark === 'success'">✓</template>
            <template v-else-if="c.mark === 'fail' || c.mark === 'blocked'">✕</template>
            <span v-else-if="c.mark === 'running'" class="pulse" />
            <template v-else>○</template>
          </span>
        </span>
        <span class="m-sub" :class="c.tone && 't-' + c.tone">{{ c.sub }}</span>
      </button>
    </div>

    <div v-else-if="!run" class="rc-skel" aria-busy="true">
      <span
        v-for="k in 4"
        :key="k"
        class="sk"
        :style="{ width: CARD_W + 'px', height: CARD_H + 'px' }"
      />
      <span class="sk-text">loading trace…</span>
    </div>

    <div v-else class="rc-empty">
      <slot name="empty">no phases recorded</slot>
    </div>

    <div
      v-if="cards.length"
      class="rc-zoom"
      @pointerdown.stop
      @click.stop
    >
      <button title="Zoom out" @click="zoomBy(1 / 1.2)">−</button>
      <span class="z-pct">{{ Math.round(view.k * 100) }}%</span>
      <button title="Zoom in" @click="zoomBy(1.2)">+</button>
      <span class="z-sep" />
      <button class="z-fit" title="Fit the whole Run" @click="fit()">Fit</button>
    </div>
  </div>
</template>

<style scoped>
.rc-canvas {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  overscroll-behavior: contain;
  cursor: grab;
  touch-action: none;
  user-select: none;
  background-color: var(--bg);
  background-image: radial-gradient(var(--dot) 1.15px, transparent 1.3px);
  outline: none;
}
.rc-canvas.grabbing {
  cursor: grabbing;
}

.rc-world {
  position: absolute;
  left: 0;
  top: 0;
  transform-origin: 0 0;
  will-change: transform;
}

.rc-links {
  position: absolute;
  left: 0;
  top: 0;
  overflow: visible;
  pointer-events: none;
}
.cn path {
  fill: none;
  stroke: var(--border-strong);
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.cn.done path {
  stroke: color-mix(in srgb, var(--ok) 60%, var(--border-strong));
}
.cn:not(.done) .e-line {
  stroke-dasharray: 3 5;
}
.cn.flowing path {
  stroke: var(--run);
}
.cn.flowing .e-line {
  stroke-dasharray: 4 5;
  animation: rc-flow 1s linear infinite;
}
.cn.broken path {
  stroke: color-mix(in srgb, var(--bad) 70%, var(--border-strong));
}
.cn.trunk .e-line {
  stroke-dasharray: none;
}
@keyframes rc-flow {
  to {
    stroke-dashoffset: -9;
  }
}

.rc-stage {
  position: absolute;
  font-size: 11px;
  font-weight: 650;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  color: var(--faint);
  pointer-events: none;
}

.rc-card {
  position: absolute;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  padding: 12px 13px 11px;
  border-radius: 15px;
  background: var(--surface);
  box-shadow: 0 1px 1px rgba(30, 30, 10, 0.04),
    0 6px 14px -4px rgba(30, 30, 10, 0.09),
    0 16px 32px -14px rgba(30, 30, 10, 0.16);
  text-align: left;
  cursor: pointer;
  transition: transform 300ms cubic-bezier(0.2, 0.8, 0.2, 1),
    box-shadow 300ms cubic-bezier(0.2, 0.8, 0.2, 1), opacity 0.2s;
}
.rc-card:hover {
  transform: translateY(-2px);
}
.rc-card.sel {
  transform: translateY(-8px) scale(1.025);
  box-shadow: 0 2px 3px rgba(30, 30, 10, 0.05),
    0 14px 26px -8px rgba(30, 30, 10, 0.16),
    0 30px 54px -20px rgba(30, 30, 10, 0.26), 0 0 0 1.5px var(--lime);
}
.rc-card.p-queued {
  opacity: 0.5;
  background: transparent;
  box-shadow: 0 0 0 1px var(--border);
}
.rc-card.p-fail {
  box-shadow: 0 1px 1px rgba(30, 30, 10, 0.04),
    0 6px 14px -4px rgba(30, 30, 10, 0.09),
    0 16px 32px -14px rgba(30, 30, 10, 0.16),
    0 0 0 1.5px color-mix(in srgb, var(--bad) 70%, transparent);
}
.rc-card.p-fail.sel {
  box-shadow: 0 2px 3px rgba(30, 30, 10, 0.05),
    0 14px 26px -8px rgba(30, 30, 10, 0.16),
    0 30px 54px -20px rgba(30, 30, 10, 0.26), 0 0 0 2px var(--bad);
}
.rc-card.p-running {
  box-shadow: 0 1px 1px rgba(30, 30, 10, 0.04),
    0 6px 14px -4px rgba(30, 30, 10, 0.09),
    0 16px 32px -14px rgba(30, 30, 10, 0.16),
    0 0 0 1.5px color-mix(in srgb, var(--run) 70%, transparent);
}
.rc-card.p-running.sel {
  box-shadow: 0 2px 3px rgba(30, 30, 10, 0.05),
    0 14px 26px -8px rgba(30, 30, 10, 0.16),
    0 30px 54px -20px rgba(30, 30, 10, 0.26), 0 0 0 2px var(--run);
}
.rc-card.p-running::before {
  content: "";
  position: absolute;
  inset: -1px;
  border-radius: 16px;
  border: 2px solid var(--run);
  pointer-events: none;
  animation: rc-breathe 2.6s ease-out infinite;
}
@keyframes rc-breathe {
  0% {
    opacity: 0.55;
    transform: scale(1);
  }
  70%,
  100% {
    opacity: 0;
    transform: scale(1.06, 1.14);
  }
}

.c-top {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.c-icon {
  flex: none;
  width: 32px;
  height: 32px;
  border-radius: 10px;
  background: var(--surface-2);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--dim);
}
.c-icon :deep(svg) {
  width: 18px;
  height: 18px;
  display: block;
}
.c-names {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}
.c-name {
  font-weight: 640;
  font-size: 13.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.c-owner {
  font-size: 11.5px;
  color: var(--dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.c-mark {
  flex: none;
  align-self: flex-start;
  display: inline-flex;
  color: var(--faint);
}
.c-mark.m-success {
  color: var(--ok);
}
.c-mark.m-fail {
  color: var(--bad);
}
.c-mark.m-blocked {
  color: var(--warn);
}
.c-mark .pulse {
  margin: 3px;
}
.pulse {
  position: relative;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--run);
  flex: none;
}
.pulse::after {
  content: "";
  position: absolute;
  inset: -3px;
  border-radius: 50%;
  border: 1.5px solid var(--run);
  opacity: 0;
  animation: rc-ring 1.8s ease-out infinite;
}
@keyframes rc-ring {
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

.c-stats {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 11px;
  color: var(--faint);
  font-variant-numeric: tabular-nums;
  min-height: 14px;
}
.cbar {
  flex: 1;
  max-width: 52px;
  height: 4px;
  border-radius: 2px;
  background: var(--border);
  overflow: hidden;
}
.cbar i {
  display: block;
  height: 100%;
  border-radius: 2px;
  background: var(--lime);
}
.cbar i.hot {
  background: var(--warn);
}
.c-time {
  margin-left: auto;
}
.c-time.live {
  color: var(--run);
  font-weight: 560;
}
.c-time.t-blocked {
  color: var(--warn);
  font-weight: 560;
}
.c-time.t-approved {
  color: var(--ok);
  font-weight: 560;
}
.c-time.t-ended {
  color: var(--bad);
  font-weight: 560;
}
.c-runs {
  font-size: 11px;
  color: var(--dim);
  font-weight: 560;
  white-space: nowrap;
}
.c-ord {
  font-weight: 500;
  color: var(--dim);
}

.c-badges {
  position: absolute;
  top: -7px;
  right: -7px;
  display: flex;
  gap: 3px;
}
.c-badges .nb {
  min-width: 18px;
  height: 18px;
  padding: 0 4px;
  border-radius: 9px;
  background: var(--warn);
  color: #fff;
  font-size: 9px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.2);
}

.rc-card.ended {
  box-shadow: 0 1px 1px rgba(30, 30, 10, 0.04),
    0 6px 14px -4px rgba(30, 30, 10, 0.09),
    0 16px 32px -14px rgba(30, 30, 10, 0.16),
    0 0 0 1.5px color-mix(in srgb, var(--bad) 70%, transparent);
}
.rc-card.ended.sel {
  box-shadow: 0 2px 3px rgba(30, 30, 10, 0.05),
    0 14px 26px -8px rgba(30, 30, 10, 0.16),
    0 30px 54px -20px rgba(30, 30, 10, 0.26), 0 0 0 2px var(--bad);
}

/* A try pill under its spine card: "Try 2 · rereview · 02:35 · 2 rounds · ✓
   approved"; a click opens and folds its column. Its y is its centre. */
.rc-pill {
  position: absolute;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 11px 0 9px;
  border-radius: 13px;
  transform: translateY(-50%);
  background: var(--surface);
  box-shadow: 0 1px 1px rgba(30, 30, 10, 0.04),
    0 6px 14px -4px rgba(30, 30, 10, 0.09);
  font-size: 12px;
  color: var(--dim);
  white-space: nowrap;
  cursor: pointer;
}
.rc-pill:hover {
  box-shadow: 0 1px 1px rgba(30, 30, 10, 0.04),
    0 6px 14px -4px rgba(30, 30, 10, 0.09), 0 0 0 1px var(--dim);
}
.rc-pill b {
  color: var(--text);
  font-weight: 650;
}
.rc-pill .sep {
  color: var(--faint);
}
.rc-pill .ok {
  color: var(--ok);
  font-weight: 600;
}
.rc-pill .bad {
  color: var(--bad);
  font-weight: 600;
}
.rc-pill.is-ok {
  box-shadow: 0 1px 1px rgba(30, 30, 10, 0.04),
    0 6px 14px -4px rgba(30, 30, 10, 0.09),
    0 0 0 1px color-mix(in srgb, var(--ok) 45%, transparent);
}
.rc-pill.is-bad {
  box-shadow: 0 1px 1px rgba(30, 30, 10, 0.04),
    0 6px 14px -4px rgba(30, 30, 10, 0.09),
    0 0 0 1px color-mix(in srgb, var(--bad) 40%, transparent);
}
.lane-tog {
  color: var(--faint);
  margin-left: 2px;
}

/* A branch run in a column: the card face at a smaller size, so the spine
   stays what you read first; " · 2nd" = its ordinal within its try. */
.rc-mini {
  position: absolute;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  padding: 8px 10px 7px;
  border-radius: 12px;
  background: var(--surface);
  box-shadow: 0 1px 1px rgba(30, 30, 10, 0.04),
    0 6px 14px -4px rgba(30, 30, 10, 0.09);
  text-align: left;
  cursor: pointer;
  transition: transform 300ms cubic-bezier(0.2, 0.8, 0.2, 1),
    box-shadow 300ms cubic-bezier(0.2, 0.8, 0.2, 1), opacity 0.2s;
}
.rc-mini:hover {
  transform: translateY(-2px);
}
.rc-mini.sel {
  transform: translateY(-5px) scale(1.03);
  box-shadow: 0 2px 3px rgba(30, 30, 10, 0.05),
    0 14px 26px -8px rgba(30, 30, 10, 0.16), 0 0 0 1.5px var(--lime);
  opacity: 1;
}
.rc-mini.ended {
  box-shadow: 0 1px 1px rgba(30, 30, 10, 0.04),
    0 6px 14px -4px rgba(30, 30, 10, 0.09),
    0 0 0 1.5px color-mix(in srgb, var(--bad) 70%, transparent);
}
.rc-mini.past {
  opacity: 0.62;
}
.rc-mini.past:hover,
.rc-mini.past.sel {
  opacity: 1;
}
.m-top {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}
.m-icon {
  flex: none;
  width: 22px;
  height: 22px;
  border-radius: 7px;
  background: var(--surface-2);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--dim);
}
.m-icon :deep(svg) {
  width: 13px;
  height: 13px;
  display: block;
}
.m-name {
  flex: 1;
  min-width: 0;
  font-weight: 640;
  font-size: 12.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.rc-mini .c-mark {
  font-size: 12px;
  align-self: center;
}
.m-sub {
  font-size: 10.5px;
  color: var(--faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-variant-numeric: tabular-nums;
}
.m-sub.t-blocked {
  color: var(--warn);
  font-weight: 560;
}
.m-sub.t-approved {
  color: var(--ok);
  font-weight: 560;
}

.rc-skel {
  position: absolute;
  left: 72px;
  top: 50%;
  display: flex;
  align-items: center;
  gap: 56px;
  transform: translateY(-50%);
}
.rc-skel .sk {
  display: inline-block;
  border-radius: 15px;
  background: var(--surface);
  box-shadow: 0 1px 1px rgba(30, 30, 10, 0.04),
    0 6px 14px -4px rgba(30, 30, 10, 0.09);
  opacity: 0.6;
  animation: rc-skel 1.2s ease-in-out infinite alternate;
}
@keyframes rc-skel {
  to {
    opacity: 0.3;
  }
}
.rc-skel .sk-text {
  font-size: 12px;
  color: var(--faint);
}

.rc-empty {
  position: absolute;
  left: 50%;
  top: 52%;
  transform: translate(-50%, -50%);
  padding: 22px 28px;
  border-radius: 14px;
  text-align: center;
  color: var(--faint);
  font-size: 13px;
}

.rc-zoom {
  position: absolute;
  right: 14px;
  bottom: 14px;
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 3px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--surface) 86%, transparent);
  box-shadow: 0 1px 2px rgba(30, 30, 10, 0.05),
    0 10px 30px -10px rgba(30, 30, 10, 0.18);
  border: 1px solid var(--border);
  backdrop-filter: blur(14px) saturate(1.2);
  z-index: 2;
}
.rc-zoom button {
  height: 26px;
  min-width: 26px;
  padding: 0 6px;
  border-radius: 7px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--dim);
  font-size: 12px;
  font-weight: 540;
  transition: background 0.12s, color 0.12s;
}
.rc-zoom button:hover {
  background: var(--surface-2);
  color: var(--text);
}
.rc-zoom .z-pct {
  min-width: 40px;
  text-align: center;
  font-size: 11.5px;
  color: var(--faint);
  font-variant-numeric: tabular-nums;
}
.rc-zoom .z-sep {
  width: 1px;
  height: 14px;
  background: var(--border);
  margin: 0 3px;
}
</style>
