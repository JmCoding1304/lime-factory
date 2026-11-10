<script setup lang="ts">
// The Settings surface: a centred dialog with a Cost / Profiles / Appearance
// nav. Mounted by Workspace inside `.ws`, so it inherits the token map. No
// transform/filter/backdrop-filter/contain/will-change on the layer, the dialog
// or `.set-body`, so ProfilesPage's `position: fixed` popover keeps viewport
// coordinates.
import { onMounted, ref } from "vue";
import CostPage from "./CostPage.vue";
import ProfilesPage from "./ProfilesPage.vue";
import { SETTINGS_SECTIONS, settingsBindings, type SettingsSection } from "../lib/settings";
import { useKeys, type KeyEventLike } from "../lib/keymap";
import { setTheme, theme, type Theme } from "../lib/theme";

const props = defineProps<{
  project: string | null;
  projects: string[];
  section: SettingsSection;
}>();
const emit = defineEmits<{
  "update:section": [SettingsSection];
  close: [];
}>();

const dialogEl = ref<HTMLElement | null>(null);
const profilesEl = ref<InstanceType<typeof ProfilesPage> | null>(null);

/** Esc: the shell asks us first. Profiles gets the event, else we close. */
function escape(e: KeyEventLike): boolean {
  return props.section === "profiles"
    ? profilesEl.value?.escape(e) ?? false
    : false;
}

defineExpose({ escape });

useKeys(settingsBindings((s) => emit("update:section", s)));

// Focus the dialog so keys target it — the Settings button behind the scrim
// cannot be re-clicked by Space/Enter.
onMounted(() => dialogEl.value?.focus());

const THEMES: { id: Theme; label: string }[] = [
  { id: "system", label: "System" },
  { id: "light", label: "Light" },
  { id: "dark", label: "Dark" },
];
</script>

<template>
  <div class="set-layer">
    <div class="set-scrim" @mousedown="emit('close')" />
    <div
      ref="dialogEl"
      class="set-dialog"
      role="dialog"
      aria-modal="true"
      aria-label="Settings"
      tabindex="-1"
    >
      <nav class="set-nav">
        <div class="set-title">Settings</div>
        <button
          v-for="s in SETTINGS_SECTIONS"
          :key="s.id"
          class="set-item"
          :class="{ on: s.id === section }"
          @click="emit('update:section', s.id)"
        >
          <span class="set-label">{{ s.label }}</span>
          <kbd>{{ s.key }}</kbd>
        </button>
        <button class="set-close" title="Close  Esc" @click="emit('close')">
          esc
        </button>
      </nav>

      <div class="set-body">
        <div v-if="section === 'cost'" class="set-scroll">
          <CostPage :project="project ?? 'all'" />
        </div>
        <div v-else-if="section === 'profiles'" class="set-profiles">
          <ProfilesPage
            ref="profilesEl"
            :project="project ?? 'all'"
            :projects="projects"
            :sync-projects="projects"
          />
        </div>
        <div v-else class="set-scroll set-appearance">
          <h2>Appearance</h2>
          <p class="set-sub">Stored in this browser.</p>
          <div class="tiles">
            <button
              v-for="t in THEMES"
              :key="t.id"
              class="tile"
              :class="{ on: theme === t.id }"
              @click="setTheme(t.id)"
            >
              <span class="tile-k">{{ t.label }}</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.set-layer {
  position: fixed;
  inset: 0;
  z-index: 70;
  display: flex;
  align-items: center;
  justify-content: center;
}
/* The blur is allowed only here: the scrim is a sibling of the dialog and an
   ancestor of nothing. */
.set-scrim {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.32);
  backdrop-filter: blur(2px);
}
.set-dialog {
  position: relative;
  width: min(1240px, calc(100vw - 48px));
  height: min(860px, calc(100vh - 48px));
  display: grid;
  grid-template-columns: 200px minmax(0, 1fr);
  background: var(--panel);
  border-radius: 16px;
  box-shadow: var(--shadow-pop);
  overflow: hidden;
  outline: none;
}

/* ── nav (proto styles.css:441-458, minus the translate) ────────────────────── */
.set-nav {
  position: relative;
  background: var(--panel-2);
  border-right: 1px solid var(--line-soft);
  padding: 22px 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.set-title {
  font-size: 15px;
  font-weight: 650;
  padding: 0 10px 14px;
  letter-spacing: -0.01em;
}
.set-item {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 34px;
  padding: 0 8px 0 10px;
  border-radius: 8px;
  color: var(--muted);
  font-weight: 520;
  transition: background 0.12s, color 0.12s;
}
.set-item:hover {
  background: var(--hover);
  color: var(--text);
}
.set-item.on {
  background: var(--panel);
  color: var(--text);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06), 0 0 0 0.5px var(--line);
}
.set-label {
  flex: 1;
  text-align: left;
}
.set-close {
  position: absolute;
  top: 14px;
  right: 14px;
  height: 26px;
  padding: 0 9px;
  border-radius: 7px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 540;
  transition: background 0.12s, color 0.12s;
}
.set-close:hover {
  background: var(--hover);
  color: var(--text);
}

/* ── body: definite height, no containing-block properties ──────────────────── */
.set-body {
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.set-scroll {
  flex: 1 1 0;
  min-height: 0;
  overflow: auto;
  scrollbar-width: thin;
}
.set-profiles {
  flex: 1 1 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ── appearance ─────────────────────────────────────────────────────────────── */
.set-appearance {
  padding: 30px 36px 36px;
}
.set-appearance h2 {
  font-size: 20px;
  font-weight: 660;
  letter-spacing: -0.015em;
  margin: 0 0 4px;
}
.set-sub {
  margin: 0 0 22px;
  color: var(--muted);
  font-size: 13px;
}
.tiles {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  max-width: 620px;
}
.tile {
  border-radius: 12px;
  background: var(--panel-2);
  box-shadow: inset 0 0 0 1px var(--line-soft);
  padding: 16px;
  text-align: left;
  transition: box-shadow 0.12s, background 0.12s;
}
.tile:hover {
  background: var(--hover);
}
.tile.on {
  box-shadow: inset 0 0 0 2px var(--lime, var(--text));
}
.tile-k {
  font-size: 13px;
  font-weight: 560;
}
</style>
