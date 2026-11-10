<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import type { Dock, ProfileCard, RosterCell } from "../lib/profilesPage";
import { cardLabel, money, selectedExtraCount, syncStatusLabel, trayFace, type SyncTarget } from "../lib/profilesPage";
import type { TrayModel } from "../lib/modelTray";
import type { ProfileSyncResult } from "../lib/types";
import { modelColor, modelLogo, modelName } from "../lib/models";
import ModelPicker from "./ModelPicker.vue";

const props = defineProps<{
  dock: Dock;
  tray: boolean;
  trayQ: string;
  selected: string;
  cards: ProfileCard[];
  models: TrayModel[];
  notionalOn: boolean;
  armed: string | null;
  addRole: string | null;
  roster: RosterCell[];
  syncTargets: SyncTarget[];
  syncResults: ProfileSyncResult[] | null;
  syncSaving: boolean;
  currentProject: string;
}>();

const emit = defineEmits<{
  "update:dock": [dock: Dock];
  "update:tray": [tray: boolean];
  "update:trayQ": [q: string];
  select: [name: string];
  delete: [name: string];
  create: [payload: { name: string; copy: boolean }];
  fab: [];
  "chip-click": [id: string];
  "pick-role": [name: string];
  "pick-model": [id: string];
  "toggle-sync-target": [name: string];
}>();

const chips = computed(() => trayFace(props.models, props.trayQ));

const extraCount = computed(() => selectedExtraCount(props.syncTargets));

// new-profile form
const nameIn = ref("");
const copy = ref(true);
const npEl = ref<HTMLInputElement | null>(null);
const trayQEl = ref<HTMLInputElement | null>(null);

watch(
  () => props.tray,
  (t) => {
    if (t) void nextTick(() => trayQEl.value?.focus());
  },
);
watch(
  () => props.dock,
  (d) => {
    if (d === "new-profile") void nextTick(() => npEl.value?.focus());
  },
);

function create() {
  emit("create", { name: nameIn.value, copy: copy.value });
  nameIn.value = "";
}

function chipMeta(c: TrayModel): string {
  if (c.cc && !props.notionalOn) return "sub";
  if (c.runs > 0) return money(c.avg);
  return c.provider;
}

/** chips drag onto blocks as plain text ids */
function onDragStart(e: DragEvent, id: string) {
  e.dataTransfer?.setData("text/plain", id);
  if (e.dataTransfer) e.dataTransfer.effectAllowed = "copy";
}
</script>

<template>
  <div class="dockwrap">
    <div
      v-if="
        dock === 'profiles' ||
        dock === 'new-profile' ||
        dock === 'add-role' ||
        dock === 'add-model'
      "
      class="dock-panel"
    >
      <template v-if="dock === 'profiles'">
        <div class="panel-head">
          <h5>profiles</h5>
          <span class="faint mono">select loads the bench · × deletes</span>
        </div>
        <div class="pcards">
          <div v-for="c in cards" :key="c.name" class="pcard-wrap">
            <button class="pcard" :class="{ on: c.name === selected }" @click="emit('select', c.name)">
              <span class="cluster">
                <span
                  v-for="m in c.models"
                  :key="m"
                  class="mini-logo"
                  :style="{ color: modelColor(m) }"
                  :title="m"
                  v-html="modelLogo(m)"
                />
                <span v-if="!c.models.length" class="mini-logo empty-dot">·</span>
              </span>
              <span class="pcard-main">
                <b>{{ c.name }}<small v-if="c.name === 'default'">roster</small></b>
                <small class="mono">
                  {{ cardLabel(c) }}
                </small>
              </span>
            </button>
            <button v-if="c.name !== 'default'" class="pdel" title="delete profile" @click.stop="emit('delete', c.name)">×</button>
          </div>
          <button class="pnew" @click="emit('update:dock', 'new-profile')">＋ new profile</button>
        </div>
      </template>
      <template v-else-if="dock === 'new-profile'">
        <div class="panel-head">
          <h5>new profile</h5>
          <button class="mini" @click="emit('update:dock', 'profiles')">‹ profiles</button>
        </div>
        <div class="pform">
          <input
            ref="npEl"
            v-model="nameIn"
            class="pp-search"
            placeholder="cheap-fast … (a-z 0-9 -)"
            autocomplete="off"
            spellcheck="false"
            @keydown.enter.prevent="create"
          />
          <label class="pp-check"><input v-model="copy" type="checkbox" /> duplicate <b>{{ selected }}</b></label>
          <div class="pp-pop-row"><button class="mini lime" @click="create">create</button></div>
        </div>
      </template>
      <template v-else-if="dock === 'add-role'">
        <div class="panel-head">
          <h5>add agent — pick a role</h5>
          <span class="faint mono">the agents: roster of .lime/config.yaml</span>
        </div>
        <div class="roles">
          <button
            v-for="c in roster"
            :key="c.name"
            class="role-btn"
            :class="{ has: c.onBench }"
            @click="emit('pick-role', c.name)"
          >
            <span class="role-head"><span class="roledot" :style="{ background: c.color }" /><b>{{ c.name }}</b></span>
            <small>{{ c.purpose }}</small>
            <em v-if="c.onBench" class="has-tag">on bench — swaps</em>
          </button>
        </div>
      </template>
      <template v-else-if="dock === 'add-model'">
        <div class="panel-head">
          <h5>add agent — model for {{ addRole }}</h5>
          <button class="mini" @click="emit('update:dock', 'add-role')">‹ roles</button>
        </div>
        <ModelPicker :models="models" :notional-on="notionalOn" autofocus @pick="emit('pick-model', $event)" />
      </template>

      <!-- sync panel (step 3) -->
      <template v-else-if="dock === 'sync'">
        <div class="panel-head">
          <h5>sync model changes</h5>
          <span class="faint mono">only subsequent model picks are copied</span>
        </div>
        <div class="sync-targets">
          <div
            v-for="st in syncTargets"
            :key="st.name"
            class="sync-row"
            :class="{ disabled: st.name === currentProject || syncSaving }"
          >
            <label class="sync-check">
              <input
                type="checkbox"
                :checked="st.selected"
                :disabled="st.name === currentProject || syncSaving"
                @change="emit('toggle-sync-target', st.name)"
              />
              <span class="sync-name">{{ st.name }}</span>
              <span v-if="st.name === currentProject" class="sync-tag faint mono">current</span>
            </label>
            <span class="sync-status" :class="st.status">{{ syncStatusLabel(st.status) }}</span>
            <span v-if="st.message" class="sync-msg mono">{{ st.message }}</span>
          </div>
        </div>
        <div v-if="syncSaving" class="sync-saving mono">saving to selected repos…</div>
      </template>
    </div>

    <div class="dock-bar" :class="{ 'mode-tray': tray }">
      <template v-if="!tray">
        <button
          class="bar-tab"
          :class="{ on: dock !== 'closed' }"
          title="profiles — p"
          @click="emit('update:dock', dock === 'profiles' ? 'closed' : 'profiles')"
        >
          Profiles <span class="bar-count">{{ cards.length }}</span>
        </button>
        <button class="bar-fab" title="add" aria-label="add" @click="emit('fab')">＋</button>
        <button
          class="bar-tab"
          :class="{ on: dock === 'sync' }"
          title="sync model changes to other repos"
          @click="emit('update:dock', dock === 'sync' ? 'closed' : 'sync')"
        >
          Sync <span v-if="extraCount > 0" class="bar-count">{{ extraCount }}</span>
        </button>
        <button class="bar-tab" title="models tray" @click="emit('update:tray', true)">Models</button>
      </template>
      <template v-else>
        <button class="bar-back" title="back — Esc" aria-label="close tray" @click="emit('update:tray', false)">‹</button>
        <div class="tray-chips">
          <button
            v-for="c in chips"
            :key="c.id"
            class="tchip"
            :class="{ armed: armed === c.id }"
            :title="`${c.note} · ${c.id}`"
            draggable="true"
            @click="emit('chip-click', c.id)"
            @dragstart="onDragStart($event, c.id)"
          >
            <span class="role-logo" :style="{ color: modelColor(c.id) }" v-html="modelLogo(c.id)" />
            <span class="tchip-name">{{ modelName(c.id) }}<em v-if="c.cc" class="cc">cc</em></span>
            <span class="tchip-cost mono">{{ chipMeta(c) }}</span>
          </button>
          <span v-if="!chips.length" class="tray-none faint mono">no model matches</span>
        </div>
        <input
          ref="trayQEl"
          class="tray-q mono"
          :value="trayQ"
          placeholder="search — /"
          aria-label="search models"
          autocomplete="off"
          spellcheck="false"
          @input="emit('update:trayQ', ($event.target as HTMLInputElement).value)"
        />
      </template>
    </div>
  </div>
</template>

<style scoped>
.dockwrap {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 18px;
  display: flex;
  justify-content: center;
  z-index: 60;
  pointer-events: none;
}

.dock-panel {
  position: absolute;
  bottom: 72px;
  left: 50%;
  transform: translateX(-50%) translateY(10px);
  width: min(600px, calc(100% - 40px));
  max-height: min(440px, 58vh);
  overflow-y: auto;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: var(--shadow);
  padding: 12px 14px;
  pointer-events: auto;
  animation: panel-in 0.16s ease;
}

@keyframes panel-in {
  from {
    opacity: 0;
    transform: translateX(-50%) translateY(10px);
  }
  to {
    opacity: 1;
    transform: translateX(-50%) translateY(0);
  }
}

.dock-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 999px;
  box-shadow: var(--shadow);
  padding: 8px;
  height: 56px;
  width: min(460px, calc(100% - 40px));
  pointer-events: auto;
  transition: width 0.22s cubic-bezier(0.2, 0.7, 0.2, 1);
}

.dock-bar.mode-tray {
  width: min(720px, calc(100% - 40px));
}

.bar-tab {
  flex: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  height: 40px;
  border-radius: 999px;
  color: var(--dim);
  font-size: 13px;
  font-weight: 500;
  white-space: nowrap;
}

.bar-tab:hover {
  background: var(--surface-2);
  color: var(--text);
}

.bar-tab.on {
  background: var(--surface-2);
  color: var(--text);
}

.bar-tab.on::before {
  content: "";
  width: 6px;
  height: 6px;
  border-radius: 2px;
  background: var(--lime);
}

.bar-count {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--faint);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 0 6px;
  line-height: 1.5;
}

.bar-fab {
  flex: none;
  width: 40px;
  height: 40px;
  border-radius: 50%;
  background: var(--lime);
  color: var(--ink);
  font-size: 19px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-shadow: var(--shadow);
  transition: transform 0.15s ease;
}

.bar-fab:hover {
  transform: scale(1.07);
}

.bar-fab:active {
  transform: scale(0.95);
}

.bar-back {
  flex: none;
  width: 36px;
  height: 36px;
  border-radius: 50%;
  color: var(--dim);
  font-size: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.bar-back:hover {
  background: var(--surface-2);
  color: var(--text);
}

.tray-chips {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 6px;
  overflow-x: auto;
  min-width: 0;
  scrollbar-width: thin;
  padding: 2px;
}

.tray-none {
  font-size: 11.5px;
  padding: 0 8px;
  white-space: nowrap;
}

.tchip {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 5px 10px 5px 5px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  cursor: pointer;
  transition: transform 0.12s ease, border-color 0.12s ease;
}

.tchip:hover {
  border-color: var(--border-strong);
  transform: translateY(-1px);
}

.tchip.armed {
  border-color: var(--lime);
  box-shadow: 0 0 0 1px var(--lime);
}

.tchip .role-logo {
  width: 22px;
  height: 22px;
  border-radius: 6px;
}

.tchip .role-logo svg {
  width: 13px;
  height: 13px;
}

.tchip-name {
  font-size: 12.5px;
  font-weight: 500;
  white-space: nowrap;
}

em.cc {
  font-style: normal;
  font-family: var(--mono);
  font-size: 9px;
  border: 1px solid var(--border-strong);
  border-radius: 4px;
  padding: 0 4px;
  margin-left: 4px;
  color: var(--lime-ink);
}

.tchip-cost {
  font-size: 10.5px;
  color: var(--faint);
  white-space: nowrap;
}

.tray-q {
  width: 150px;
  flex: none;
  padding: 7px 13px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--text);
  font-family: var(--mono);
  font-size: 12px;
  outline: none;
}

.tray-q:focus {
  border-color: var(--purple);
}

/* role grid — the add flow, step 1 (lifted from proto.css) */
.roles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 8px;
}

.role-btn {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
  padding: 10px 11px;
  border: 1px solid var(--border);
  border-radius: 10px;
  text-align: left;
  background: var(--surface);
}

.role-btn:hover {
  border-color: var(--border-strong);
  background: var(--surface-2);
}

.role-btn b {
  font-size: 13px;
}

.role-btn small {
  font-size: 10.5px;
  color: var(--faint);
  line-height: 1.45;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.role-btn.has {
  border-style: dashed;
}

.role-head {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.roledot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  display: inline-block;
  flex: none;
}

.has-tag {
  position: absolute;
  top: 8px;
  right: 8px;
  font-style: normal;
  font-family: var(--mono);
  font-size: 8.5px;
  color: var(--lime-ink);
  border: 1px solid var(--lime);
  border-radius: 999px;
  padding: 0 5px;
  white-space: nowrap;
}

/* ── panel content ────────────────────────────────────────────────────────── */

.panel-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}

.panel-head h5 {
  margin: 0;
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.09em;
  color: var(--faint);
  font-weight: 500;
}

.panel-head .faint {
  font-size: 10.5px;
}

.pcards {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.pcard-wrap {
  display: flex;
  align-items: center;
  gap: 2px;
}

.pcard {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1;
  min-width: 0;
  padding: 9px 11px;
  border: 1px solid var(--border);
  border-radius: 10px;
  text-align: left;
  background: var(--surface);
}

.pcard:hover {
  background: var(--surface-2);
}

.pcard.on {
  border-color: var(--lime);
  box-shadow: 0 0 0 1px var(--lime);
}

.cluster {
  display: inline-flex;
  flex: none;
}

.mini-logo {
  display: inline-flex;
  background: var(--surface-2);
  border-radius: 5px;
  padding: 3px;
}

.mini-logo + .mini-logo {
  margin-left: -6px;
}

.mini-logo svg {
  width: 12px;
  height: 12px;
  display: block;
}

.mini-logo.empty-dot {
  color: var(--faint);
  font-size: 11px;
  line-height: 12px;
}

.pcard-main {
  min-width: 0;
  flex: 1;
}

.pcard-main b {
  display: block;
  font-size: 13px;
  font-weight: 600;
}

.pcard-main b small {
  font-family: var(--mono);
  font-size: 9.5px;
  color: var(--faint);
  margin-left: 5px;
  font-weight: 400;
}

.pcard-main small {
  display: block;
  font-size: 10.5px;
  color: var(--dim);
}

.pdel {
  color: var(--faint);
  font-size: 14px;
  padding: 4px 8px;
  flex: none;
}

.pdel:hover {
  color: var(--bad);
}

.pnew {
  padding: 10px;
  border: 1px dashed var(--border-strong);
  border-radius: 10px;
  color: var(--dim);
  font-size: 13px;
}

.pnew:hover {
  color: var(--text);
  border-color: var(--faint);
}

.pform {
  padding: 2px 0 4px;
}

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

.pp-check {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--dim);
  margin: 8px 0;
}

.pp-check b {
  color: var(--text);
}

.pp-pop-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 10px;
}

.mini {
  padding: 4px 10px;
  border-radius: 7px;
  border: 1px solid var(--border);
  color: var(--dim);
  font-size: 12px;
}

.mini:hover {
  border-color: var(--border-strong);
  color: var(--text);
}

.mini.lime {
  background: var(--lime);
  border-color: var(--lime);
  color: var(--ink);
  font-weight: 600;
}

/* ── sync panel (step 3) ───────────────────────────────────────────── */

.sync-targets {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.sync-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  flex-wrap: wrap;
}

.sync-row.disabled {
  opacity: 0.6;
}

.sync-check {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
  cursor: pointer;
}

.sync-check input[type="checkbox"] {
  flex: none;
}

.sync-name {
  font-size: 13px;
  font-weight: 500;
}

.sync-tag {
  font-size: 10px;
}

.sync-status {
  font-size: 11px;
  font-family: var(--mono);
  white-space: nowrap;
  flex: none;
}

.sync-status.success {
  color: var(--lime-ink);
}

.sync-status.noop {
  color: var(--dim);
}

.sync-status.error {
  color: var(--bad);
}

.sync-status.saving {
  color: var(--purple);
}

.sync-msg {
  font-size: 10px;
  color: var(--faint);
  width: 100%;
  padding-left: 20px;
}

.sync-saving {
  margin-top: 8px;
  font-size: 11px;
  color: var(--purple);
}
</style>