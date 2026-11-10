<script setup lang="ts">
// The read-only terminal. All text and rules live in `lib/terminal.ts`; this
// file wires scroll, drag and storage. Mounted by RunView (267).

import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch,
} from "vue";
import type { FeedEvent } from "../lib/types";
import {
  clampTermHeight,
  emptyText,
  jumpLabel,
  loadTermPrefs,
  onNewLines,
  PAGE,
  saveTermCollapsed,
  saveTermHeight,
  showEarlier,
  TERM_FALLBACK_H,
  TERM_HEAD,
  terminalLines,
  windowLines,
  type TermLine,
} from "../lib/terminal";

const props = withDefaults(
  defineProps<{ events: FeedEvent[]; phaseId?: string | null; queued?: boolean }>(),
  { phaseId: null, queued: false },
);

const lines = computed(() => terminalLines(props.events, props.phaseId));
const limit = ref(PAGE);
const unseen = ref(0);
const following = ref(true);
const win = computed(() => windowLines(lines.value, limit.value));
const opened = reactive(new Set<number>());

/** localStorage can throw on access (private mode, blocked cookies). */
function store(): Pick<Storage, "getItem" | "setItem"> | null {
  try {
    return globalThis.localStorage;
  } catch {
    return null;
  }
}

const prefs = loadTermPrefs(store());
const collapsed = ref(prefs.collapsed);
const height = ref(prefs.height ?? TERM_FALLBACK_H);
const dragging = ref(false);

const root = ref<HTMLElement | null>(null);
const body = ref<HTMLElement | null>(null);

let programmatic = false;
let jumpTimer: ReturnType<typeof setTimeout> | null = null;
let dragMove: ((ev: PointerEvent) => void) | null = null;
let dragStop: (() => void) | null = null;

function parentH(): number {
  return root.value?.parentElement?.clientHeight ?? 0;
}

/** True while the reader sits within a line of the tail. */
function atBottom(): boolean {
  const el = body.value;
  if (!el) return true;
  return el.scrollHeight - el.scrollTop - el.clientHeight < 24;
}

function toBottom(smooth = false): void {
  const el = body.value;
  if (!el) return;
  el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
}

function onScroll(): void {
  if (programmatic) return;
  following.value = atBottom();
  if (following.value) unseen.value = 0;
}

onMounted(() => {
  height.value = clampTermHeight(prefs.height, parentH());
  nextTick(toBottom);
});

watch(
  () => lines.value.length,
  (now, before) => {
    const added = now - before;
    if (added <= 0) return;
    const next = onNewLines(
      { limit: limit.value, unseen: unseen.value },
      added,
      following.value,
    );
    limit.value = next.limit;
    unseen.value = next.unseen;
    if (following.value) nextTick(toBottom);
  },
);

watch(
  () => props.phaseId,
  () => {
    opened.clear();
    limit.value = PAGE;
    unseen.value = 0;
    following.value = true;
    nextTick(toBottom);
  },
);

function jumpLatest(): void {
  following.value = true;
  unseen.value = 0;
  programmatic = true;
  toBottom(true);
  if (jumpTimer !== null) clearTimeout(jumpTimer);
  jumpTimer = setTimeout(() => {
    programmatic = false;
    jumpTimer = null;
  }, 600);
}

/** A line opens when it carries a call, an output tail or meta facts. */
function hasMore(l: TermLine): boolean {
  return !!(l.detail?.length || l.out?.length || l.meta.length);
}

function flip(id: number): void {
  if (opened.has(id)) opened.delete(id);
  else opened.add(id);
}

function loadEarlier(): void {
  const el = body.value;
  const fromBottom = el ? el.scrollHeight - el.scrollTop : 0;
  limit.value = showEarlier(limit.value);
  nextTick(() => {
    const e = body.value;
    if (e) e.scrollTop = e.scrollHeight - fromBottom;
  });
}

function toggle(): void {
  collapsed.value = !collapsed.value;
  saveTermCollapsed(store(), collapsed.value);
  if (!collapsed.value) nextTick(toBottom);
}

/** Opening the dock un-collapses a collapsed terminal (NOTES §4). */
function expand(): void {
  if (collapsed.value) toggle();
}

defineExpose({ expand });

function splitStart(e: PointerEvent): void {
  if (e.button !== 0) return;
  e.preventDefault();
  const y0 = e.clientY;
  const h0 = collapsed.value ? TERM_HEAD : height.value;
  collapsed.value = false;
  dragging.value = true;

  dragMove = (ev: PointerEvent) => {
    height.value = clampTermHeight(h0 - (ev.clientY - y0), parentH());
  };
  dragStop = () => {
    window.removeEventListener("pointermove", dragMove!);
    window.removeEventListener("pointerup", dragStop!);
    window.removeEventListener("pointercancel", dragStop!);
    dragMove = null;
    dragStop = null;
    dragging.value = false;
    saveTermHeight(store(), height.value);
    saveTermCollapsed(store(), false);
  };
  window.addEventListener("pointermove", dragMove);
  window.addEventListener("pointerup", dragStop);
  window.addEventListener("pointercancel", dragStop);
}

onBeforeUnmount(() => {
  if (dragMove) window.removeEventListener("pointermove", dragMove);
  if (dragStop) {
    window.removeEventListener("pointerup", dragStop);
    window.removeEventListener("pointercancel", dragStop);
  }
  dragMove = null;
  dragStop = null;
  if (jumpTimer !== null) {
    clearTimeout(jumpTimer);
    jumpTimer = null;
  }
});
</script>

<template>
  <section
    ref="root"
    class="term"
    :class="{ collapsed, dragging }"
    :style="{ height: (collapsed ? TERM_HEAD : height) + 'px' }"
    aria-label="Run output (read-only)"
  >
    <div
      class="term-split"
      title="Drag to resize, double-click to collapse"
      @pointerdown="splitStart"
      @dblclick="toggle"
    ></div>
    <div class="term-main">
      <header class="term-head" @dblclick="toggle">
        <slot name="bar" />
        <span class="th-fill"></span>
        <span class="th-ro" title="Read-only: a recording of the Run">read-only</span>
        <button
          class="th-btn"
          :class="{ up: collapsed }"
          :title="collapsed ? 'Show output' : 'Hide output'"
          @click.stop="toggle"
        >
          <svg
            viewBox="0 0 24 24"
            width="14"
            height="14"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <path d="m6 9 6 6 6-6" />
          </svg>
        </button>
      </header>
      <div v-show="!collapsed" ref="body" class="term-body" @scroll="onScroll">
        <div class="term-lines">
          <button
            v-if="win.hidden > 0"
            class="earlier"
            :title="`${win.hidden} earlier lines`"
            @click="loadEarlier"
          >
            Show earlier
          </button>
          <template v-for="l in win.shown" :key="l.id">
            <div
              class="tl"
              :class="['k-' + l.kind, { click: hasMore(l), open: opened.has(l.id) }]"
              @click="hasMore(l) && flip(l.id)"
            >
              <span class="tl-ts">{{ l.ts }}</span>
              <span class="tl-main">
                <template v-if="l.kind === 'cmd'"><span class="tl-prompt">❯</span><span
                    v-if="l.tool"
                    class="tl-tool"
                    :class="l.toolCls"
                  >{{ l.tool }}</span><span class="tl-cmd">{{ l.text }}</span></template>
                <span v-else-if="l.kind === 'rule'" class="tl-rule">── {{ l.text }} ──</span>
                <span v-else-if="l.kind === 'comment'" class="tl-text"># {{ l.text }}</span>
                <span v-else class="tl-text">{{ l.text }}</span>
              </span>
              <span
                v-if="l.ok !== null || l.right"
                class="tl-right"
                :class="{ good: l.ok === true, bad: l.ok === false }"
              >{{ l.ok === true ? '✓ ' : l.ok === false ? '✕ ' : '' }}{{ l.right }}</span>
            </div>
            <div v-if="opened.has(l.id)" class="tl-detail">
              <div
                v-for="(d, j) in l.detail ?? []"
                :key="'d' + j"
                class="td"
              >{{ d || ' ' }}</div>
              <div
                v-for="(o, j) in l.out ?? []"
                :key="'o' + j"
                class="td td-out"
                :class="{ 'td-first': j === 0 }"
              >{{ o || ' ' }}</div>
              <div
                v-for="(m, j) in l.meta"
                :key="'m' + j"
                class="td td-meta"
              >{{ m }}</div>
            </div>
          </template>
          <div v-if="!lines.length" class="tl k-comment">
            <span class="tl-ts"></span>
            <span class="tl-main"><span class="tl-text"># {{ emptyText(phaseId, queued) }}</span></span>
          </div>
        </div>
      </div>
      <button v-if="!following && !collapsed" class="jump" @click="jumpLatest">
        {{ jumpLabel(unseen) }}
      </button>
    </div>
    <slot name="dock" />
  </section>
</template>

<style scoped>
.term {
  --t-bg: #111214;
  --t-head: #18191c;
  --t-text: #d5d8d1;
  --t-dim: #8a8f86;
  --t-faint: #5b6058;
  --t-line: rgba(255, 255, 255, .06);
  --t-green: #a3e635;
  --t-cyan: #67e8f9;
  --t-magenta: #f0abfc;
  --t-yellow: #fde68a;
  --t-blue: #93c5fd;
  --t-red: #fca5a5;
  --t-orange: #fdba74;
  background: var(--t-bg);
  color: var(--t-text);
  color-scheme: dark;
  position: relative;
  display: flex;
  flex-direction: row;
  min-height: 0;
  flex: none;
  transition: height 320ms cubic-bezier(.32, .72, 0, 1);
}
.term.dragging { transition: none; }
.term-main {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  position: relative;
}
.term-split {
  position: absolute;
  top: -3px;
  left: 0;
  right: 0;
  height: 7px;
  z-index: 3;
  cursor: row-resize;
}
.term-split::after {
  content: "";
  position: absolute;
  left: 50%;
  top: 2px;
  width: 36px;
  height: 3px;
  margin-left: -18px;
  border-radius: 2px;
  background: rgba(255, 255, 255, 0);
  transition: background .15s;
}
.term-split:hover::after,
.term.dragging .term-split::after { background: var(--t-blue); }
.term-head {
  flex: none;
  height: 34px;
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 0 8px 0 14px;
  background: var(--t-head);
  border-bottom: 1px solid var(--t-line);
  font-size: 12px;
  color: var(--t-dim);
  white-space: nowrap;
  user-select: none;
}
.th-fill { flex: 1; }
.th-ro {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 20px;
  padding: 0 7px;
  border-radius: 5px;
  border: 1px solid var(--t-line);
  color: var(--t-faint);
  font-size: 11px;
}
.th-btn {
  width: 26px;
  height: 26px;
  padding: 0;
  border: 0;
  background: none;
  border-radius: 6px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--t-dim);
  cursor: pointer;
  transition: background .12s, transform .25s cubic-bezier(.32, .72, 0, 1);
}
.th-btn:hover { background: rgba(255, 255, 255, .07); color: var(--t-text); }
.th-btn.up { transform: rotate(180deg); }
.term.collapsed .term-head { border-bottom-color: transparent; cursor: pointer; }
.term-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  display: flex;
  flex-direction: column;
  scrollbar-width: thin;
  scrollbar-color: rgba(255, 255, 255, .14) transparent;
}
.term-lines {
  margin-top: auto;
  padding: 8px 0 10px;
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.7;
}
.earlier {
  display: block;
  width: 100%;
  text-align: left;
  padding: 4px 14px;
  border: 0;
  background: none;
  color: var(--t-dim);
  font-family: var(--mono);
  font-size: 11px;
  cursor: pointer;
}
.earlier:hover { color: var(--t-text); }
.tl { display: flex; align-items: baseline; gap: 12px; padding: 0 14px; white-space: nowrap; min-width: 0; }
.tl.click { cursor: pointer; }
.tl.click:hover, .tl.open { background: rgba(255, 255, 255, .045); }
.tl-ts { flex: none; width: 56px; color: var(--t-faint); font-size: 11px; }
.tl-main { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.tl-prompt { color: var(--t-green); margin-right: 8px; font-weight: 700; }
.tl-cmd { color: var(--t-text); }
.tl-tool { display: inline-block; min-width: 42px; margin-right: 10px; color: var(--t-dim); }
.t-read { color: var(--t-cyan); }
.t-grep { color: var(--t-magenta); }
.t-edit { color: var(--t-yellow); }
.t-ls { color: var(--t-blue); }
.t-dim { color: var(--t-dim); }
.tl-right { flex: none; color: var(--t-faint); font-size: 11px; }
.tl-right.good { color: var(--t-green); }
.tl-right.bad { color: var(--t-red); }
.tl.k-comment .tl-text, .tl.k-rule .tl-text { color: var(--t-faint); }
.tl-rule { color: var(--t-faint); }
.tl.k-ok .tl-text { color: var(--t-green); }
.tl.k-err .tl-text { color: var(--t-red); }
.tl.k-warn .tl-text { color: var(--t-orange); }
.tl-detail {
  margin: 2px 14px 6px 82px;
  padding: 6px 12px 7px;
  border-left: 2px solid rgba(255, 255, 255, .12);
  background: rgba(255, 255, 255, .03);
  border-radius: 0 6px 6px 0;
}
.td { white-space: pre-wrap; word-break: break-word; color: var(--t-text); }
.td-out { color: var(--t-dim); }
.td-out.td-first { border-top: 1px solid var(--t-line); }
.td-meta { color: var(--t-faint); }
.jump {
  position: absolute;
  right: 18px;
  bottom: 14px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 11px 0 9px;
  border: 0;
  border-radius: 999px;
  background: #e8eae4;
  color: #16181a;
  font-size: 12px;
  font-weight: 560;
  font-family: var(--sans);
  cursor: pointer;
  box-shadow: 0 6px 18px rgba(0, 0, 0, .35);
}
.jump:hover { background: #fff; }
</style>
