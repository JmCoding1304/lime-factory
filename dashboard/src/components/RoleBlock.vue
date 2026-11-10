<script setup lang="ts">
import { computed, nextTick, ref } from "vue";
import type { Leg } from "../lib/profiles";
import type { Pos } from "../lib/bench";
import { modelColor, modelLogo, modelName } from "../lib/models";

const props = defineProps<{
  leg: Leg;
  pos: Pos;
  selected: boolean;
  dragging: boolean;
  snapped: boolean;
  flash: boolean;
  color: string;
  cost: string;
  serverErrors: string[];
  want: boolean;
}>();

const emit = defineEmits<{
  select: [];
  "pick-model": [anchor: HTMLElement];
  step: [delta: 1 | -1];
  remove: [];
  rename: [name: string];
  pointerdown: [e: PointerEvent];
  "drop-model": [id: string];
}>();

// the head name swaps to an input on click; Enter/blur commits, Escape cancels
const editing = ref(false);
const nameIn = ref("");
const nameEl = ref<HTMLInputElement | null>(null);

function startEdit() {
  nameIn.value = props.leg.role;
  editing.value = true;
  void nextTick(() => nameEl.value?.focus());
}

function commit() {
  if (!editing.value) return;
  editing.value = false;
  // empty commits as-is — the server decides whether a name is legal
  if (nameIn.value !== props.leg.role) emit("rename", nameIn.value);
}

function cancel() {
  editing.value = false;
}

// drop target for tray chips (HTML5 drag)
const dropOver = ref(false);

function onDrop(e: DragEvent) {
  dropOver.value = false;
  const id = e.dataTransfer?.getData("text/plain");
  if (id) emit("drop-model", id);
}

// entering a child fires dragleave on the block — only clear when actually
// leaving the whole block (proto drop-target contract)
function onDragLeave(e: DragEvent) {
  const to = e.relatedTarget;
  if (!(to instanceof Node) || !e.currentTarget || !(e.currentTarget as Node).contains(to)) {
    dropOver.value = false;
  }
}

// where the model comes from — the chip names the winning layer
const src = computed(() => {
  if (props.leg.src.model === "patch" || props.leg.src.runtime === "patch") {
    return { label: "patch", cls: "patch" };
  }
  if (props.leg.src.model === "profile") return { label: "defaults", cls: "def" };
  if (props.leg.src.model === "agent") return { label: "agent", cls: "" };
  if (props.leg.src.model === "config") return { label: "config", cls: "" };
  return { label: "inherit", cls: "" };
});
</script>

<template>
  <div
    class="blk"
    :class="{
      sel: selected,
      drag: dragging,
      snap: snapped,
      flash,
      want,
      drop: dropOver,
      invalid: !!leg.invalid || serverErrors.length > 0,
      inherit: !leg.patched,
    }"
    :style="{ left: pos.x + 'px', top: pos.y + 'px' }"
    :title="leg.invalid ? 'invalid — drop a legal model' : leg.model"
    @pointerdown="emit('pointerdown', $event)"
    @click="emit('select')"
    @dragenter.prevent="dropOver = true"
    @dragover.prevent="dropOver = true"
    @dragleave="onDragLeave"
    @drop.prevent="onDrop"
  >
    <div class="blk-head">
      <span class="roledot" :style="{ background: color }" />
      <button
        v-if="!editing"
        class="blk-name"
        title="click to rename"
        @click.stop="want ? emit('select') : startEdit()"
      >
        {{ leg.role }}
      </button>
      <input
        v-else
        ref="nameEl"
        v-model="nameIn"
        class="blk-name-in mono"
        aria-label="role name"
        @click.stop
        @keydown.enter.prevent="commit"
        @keydown.esc.prevent="cancel"
        @blur="commit"
      />
      <span class="chip src" :class="src.cls">{{ src.label }}</span>
      <button class="blk-x" :title="`remove ${leg.role}`" @click.stop="emit('remove')">×</button>
    </div>

    <div v-if="leg.invalid" class="blk-bad mono">! {{ leg.invalid }}</div>
    <div
      v-else
      class="blk-model"
      role="button"
      :title="want ? 'click to apply the armed model' : 'click to swap the model'"
      @click.stop="want ? emit('select') : emit('pick-model', $event.currentTarget as HTMLElement)"
    >
      <span class="role-logo" :style="{ color: modelColor(leg.model) }" v-html="modelLogo(leg.model)" />
      <span class="blk-mname">
        <b>{{ modelName(leg.model) }}</b>
        <small class="mono">{{ leg.model }}</small>
      </span>
    </div>
    <div v-for="m in serverErrors" :key="m" class="blk-bad mono">{{ m }}</div>

    <template v-if="!leg.invalid">
      <div class="blk-foot">
        <span
          class="chip rt"
          :class="{ cc: leg.runtime === 'claude_code' }"
          :title="leg.runtime === 'claude_code' ? 'claude_code — rides the subscription' : 'pi — API tokens via openrouter'"
        >{{ leg.runtime === "claude_code" ? "claude_code" : "pi" }}</span>
        <span class="step">
          <button aria-label="lower thinking" @click.stop="emit('step', -1)">−</button>
          <span class="chip think" title="thinking level">{{ leg.thinking }}</span>
          <button aria-label="raise thinking" @click.stop="emit('step', 1)">＋</button>
        </span>
      </div>
      <div class="blk-cost mono">{{ cost }}</div>
    </template>
  </div>
</template>

<style scoped>
.blk {
  position: absolute;
  z-index: 1;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: var(--shadow);
  padding: 10px 12px;
  width: 236px;
  cursor: grab;
  user-select: none;
  touch-action: none;
}

.blk:active {
  cursor: grabbing;
}

.blk.drag {
  z-index: 3;
  box-shadow: var(--shadow), 0 14px 34px rgba(26, 26, 26, 0.16);
}

.blk.snap {
  transition: left 0.13s ease, top 0.13s ease;
}

.blk.sel {
  border-color: var(--lime);
  box-shadow: 0 0 0 1px var(--lime), var(--shadow);
  z-index: 2;
}

.blk.inherit {
  border-style: dashed;
}

/* an armed chip is looking for a home — every block offers itself */
.blk.want {
  border-color: var(--lime);
  border-style: solid;
}

.blk.drop {
  border-color: var(--purple);
}

.blk.invalid {
  border-color: var(--bad);
}

.blk.flash {
  animation: blk-flash 1.1s ease-out;
}

@keyframes blk-flash {
  0% {
    background: var(--ok-chip);
  }
}

.blk-head {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 9px;
  font-size: 13px;
}

.blk-name {
  font-weight: 700;
  font-size: 13.5px;
  letter-spacing: -0.01em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.blk-name:hover {
  color: var(--text);
  text-decoration: underline dashed;
  text-underline-offset: 3px;
}

.blk-name-in {
  width: 120px;
  min-width: 0;
  font-size: 12.5px;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
  padding: 1px 6px;
  outline: none;
}

.roledot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  display: inline-block;
  flex: none;
}

.chip.src {
  color: var(--faint);
  font-size: 10px;
  padding: 1px 7px;
}

.chip.src.patch {
  border-color: var(--lime);
  color: var(--lime-ink);
}

.chip.src.def {
  border-style: dashed;
}

.blk-x {
  margin-left: auto;
  color: var(--faint);
  font-size: 15px;
  line-height: 1;
  padding: 0 2px;
}

.blk-x:hover {
  color: var(--bad);
}

.blk-model {
  display: flex;
  gap: 9px;
  align-items: center;
  padding: 7px 8px;
  margin: 0 -8px;
  border-radius: 9px;
  cursor: pointer;
}

.blk-model:hover {
  background: var(--surface-2);
}

.blk-mname {
  min-width: 0;
  flex: 1;
}

.blk-mname b {
  display: block;
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.blk-mname small {
  display: block;
  font-size: 10.5px;
  color: var(--dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.blk-bad {
  color: var(--bad);
  font-size: 11px;
  line-height: 1.5;
  padding: 10px 9px;
  border: 1.5px dashed var(--bad);
  border-radius: 9px;
}

.blk-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  margin-top: 9px;
  flex-wrap: wrap;
}

.chip.rt {
  font-size: 10.5px;
}

.chip.rt.cc {
  border: none;
  color: var(--lime-ink);
  background: color-mix(in srgb, var(--lime) 16%, transparent);
  font-weight: 500;
}

.step {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}

.step button {
  width: 18px;
  height: 18px;
  border: 1px solid var(--border);
  border-radius: 5px;
  color: var(--dim);
  font-size: 11px;
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.step button:hover {
  color: var(--text);
  border-color: var(--border-strong);
}

.chip.think {
  min-width: 58px;
  text-align: center;
}

.blk-cost {
  margin-top: 9px;
  text-align: right;
  font-size: 12px;
  color: var(--dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>