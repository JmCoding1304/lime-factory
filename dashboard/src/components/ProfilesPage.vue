<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, shallowRef, watch } from "vue";
import type { CatalogResponse, ProfileSyncResult, ProfilesResponse, SessionCost } from "../lib/types";
import { ApiValidationError, fetchCatalog, fetchCost, fetchProfiles, putProfiles, syncProfileModels } from "../lib/api";
import {
  BLOCK_H_EST,
  BLOCK_W,
  GRID,
  chainPairs,
  placeMissing,
  snap,
  wheelToView,
  wirePath,
  zoomAt,
  type Pos,
  type View,
} from "../lib/bench";
import {
  agentByName,
  benchRoles,
  clearDefaults,
  cloneDraft,
  createProfile,
  defaultsModelChange,
  deleteProfile,
  estimate,
  levelsFor,
  modelChange,
  profileNames,
  removeRole,
  renameProfile,
  renameRole,
  resolveRole,
  nameOf,
  setDefaults,
  setRole,
  stepThinking,
  str,
  type Draft,
  type Estimate,
  type Leg,
  type SlotPatch,
  type ThinkingSupport,
} from "../lib/profiles";
import {
  canvasNote,
  colorOf,
  costLabel,
  errorsForRole,
  money,
  nudge,
  popPosition,
  profileBindings,
  profileCards,
  rosterGrid,
  SHORTCUTS,
  syncTargetsInit,
  toggleSyncTarget,
  pendingChangesKey,
  coalesceChanges,
  removePendingChange,
  rekeyPendingChange,
  applySyncResults,
  targetProject,
  type SyncStatus,
  type SyncTarget,
  type PendingChange,
  type Dock,
  type Pop,
  type PopPos,
} from "../lib/profilesPage";
import { trayModels } from "../lib/modelTray";
import { modelName } from "../lib/models";
import { fmtMs } from "../lib/format";
import { useKeys, type KeyEventLike } from "../lib/keymap";
import RoleBlock from "./RoleBlock.vue";
import ProfilesDock from "./ProfilesDock.vue";
import ModelPicker from "./ModelPicker.vue";

const props = defineProps<{ project: string; projects: string[]; syncProjects: string[] }>();

// ── sync state (step 3) ────────────────────────────────────────────────────

const syncTargets = ref<SyncTarget[]>([]);
const pending = ref<PendingChange[]>([]);
const syncResults = ref<ProfileSyncResult[] | null>(null);
const syncSaving = ref(false);

// ── state ───────────────────────────────────────────────────────────────────

const server = shallowRef<ProfilesResponse | null>(null);
const draft = ref<Draft | null>(null);
const rows = shallowRef<SessionCost[]>([]);
const catalog = shallowRef<CatalogResponse | null>(null);
const selected = ref("default");
const sel = ref<string | null>(null);
const dock = ref<Dock>("closed");
const tray = ref(false);
const trayQ = ref("");
const notionalOn = ref(true);
const loadError = ref<string | null>(null);
const saveError = ref<string[] | null>(null);
const saving = ref(false);
const view = reactive<View>({ x: 0, y: 0, k: 1 });
const layouts = reactive(new Map<string, Map<string, Pos>>());
const wirePaths = ref<string[]>([]);
const toastMsg = ref("");
const flashRole = ref<string | null>(null);
const blockEls = new Map<string, HTMLElement>();
const canvasEl = ref<HTMLElement | null>(null);
const armed = ref<string | null>(null);
const addRole = ref<string | null>(null);

// the one popover host — model row, defaults block, profile rename
const pop = ref<Pop | null>(null);
const popPos = ref<PopPos>({ left: 0, top: 0 });
const popEl = ref<HTMLElement | null>(null);
let popAnchor: HTMLElement | null = null;
const renameIn = ref("");
const rnEl = ref<HTMLInputElement | null>(null);

const EMPTY_EST: Estimate = {
  legs: [],
  sum: 0,
  pAll: 1,
  wall: 0,
  per: null,
  broken: true,
  empty: true,
};

// ── loading ─────────────────────────────────────────────────────────────────

const target = computed(() => targetProject(props.project, props.projects));

// Reset sync state when the source project changes
watch(target, (t) => {
  if (t) {
    syncTargets.value = syncTargetsInit(t, props.syncProjects);
    pending.value = [];
    syncResults.value = null;
  }
}, { immediate: true });

async function load() {
  const t = target.value;
  closePop();
  if (t === null) {
    server.value = null;
    draft.value = null;
    return;
  }
  try {
    const res = await fetchProfiles(t);
    server.value = res;
    draft.value = cloneDraft(res);
    selected.value = "default";
    sel.value = null;
    saveError.value = null;
    loadError.value = null;
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : String(err);
  }
}

watch(target, () => void load(), { immediate: true });

onMounted(() => {
  // cost rows + catalog power the estimates and the tray; failures leave the
  // page working with no data
  fetchCost(90)
    .then((r) => (rows.value = r))
    .catch(() => {});
  fetchCatalog()
    .then((c) => (catalog.value = c))
    .catch(() => {});
  document.addEventListener("mousedown", onDocMouseDown);
  window.addEventListener("resize", redrawWires);
  redrawWires();
});

onUnmounted(() => {
  document.removeEventListener("mousedown", onDocMouseDown);
  window.removeEventListener("resize", redrawWires);
  // Flush a pending debounced autosave so closing the page never drops the edit.
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = undefined;
    void save();
  }
  if (toastTimer) clearTimeout(toastTimer);
  if (flashTimer) clearTimeout(flashTimer);
});

// ── derived ─────────────────────────────────────────────────────────────────

const defaults = computed(() => server.value?.defaults ?? {});
const support = computed<ThinkingSupport>(() => server.value?.thinking_support ?? {});
const bench = computed(() => (draft.value ? benchRoles(draft.value, selected.value) : []));
const est = computed(() =>
  draft.value
    ? estimate(draft.value, defaults.value, selected.value, rows.value, notionalOn.value, support.value)
    : EMPTY_EST,
);
const prof = computed(() => draft.value?.profiles[selected.value]);
const hasDefs = computed(
  () => selected.value !== "default" && Object.keys(prof.value?.defaults ?? {}).length > 0,
);

function layoutFor(name: string): Map<string, Pos> {
  let m = layouts.get(name);
  if (!m) {
    m = new Map();
    layouts.set(name, m);
  }
  return m;
}

const positions = computed(() => {
  if (!draft.value) return new Map<string, Pos>();
  return placeMissing([...(hasDefs.value ? ["__defs"] : []), ...bench.value], layoutFor(selected.value));
});

const cards = computed(() =>
  draft.value
    ? profileCards(draft.value, defaults.value, rows.value, notionalOn.value, support.value)
    : [],
);
const trayList = computed(() => trayModels(rows.value, catalog.value, trayQ.value));

const roster = computed(() =>
  draft.value ? rosterGrid(draft.value, selected.value) : [],
);

const defsRt = computed(() => str(prof.value?.defaults?.runtime) ?? "runtime inherit");
const defsModel = computed(() => str(prof.value?.defaults?.model) ?? "model inherit");
const defsThink = computed(() => str(prof.value?.defaults?.thinking) ?? "inherit");

function posOf(key: string): Pos {
  return positions.value.get(key) ?? { x: 0, y: 0 };
}

function roleColor(role: string): string {
  return colorOf(draft.value ? agentByName(draft.value, role) : undefined);
}

// ── the one write path ──────────────────────────────────────────────────────

let editSeq = 0;
let saveTimer: ReturnType<typeof setTimeout> | undefined;
let again = false;

function edit(fn: (d: Draft) => void) {
  if (!draft.value) return;
  fn(draft.value);
  editSeq++;
  markDirty();
}

function markDirty() {
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => void save(), 300);
}

async function save() {
  const d = draft.value;
  const t = target.value;
  if (!d || !t || saving.value) {
    if (saving.value) again = true;
    return;
  }
  const seq = editSeq;
  const snapshot = [...pending.value];
  const extraTargets = syncTargets.value.filter((st) => st.selected && st.name !== t);
  const hasSync = snapshot.length > 0 && extraTargets.length > 0;
  saving.value = true;
  try {
    const res = await putProfiles(t, { agents: d.agents, profiles: d.profiles });
    server.value = res;
    if (editSeq === seq) {
      draft.value = cloneDraft(res);
      if (!profileNames(draft.value).includes(selected.value)) selected.value = "default";
    }
    saveError.value = null;

    // Sync phase: after local save succeeds, synchronise to extra targets
    if (hasSync) {
      syncSaving.value = true;
      try {
        const syncRes = await syncProfileModels(t, extraTargets.map((st) => st.name), snapshot.map((p) => p.change));
        syncResults.value = syncRes.results;
        syncTargets.value = applySyncResults(syncTargets.value, syncRes.results);
        // Remove only the snapshot keys so edits made during save stay queued
        const snapshotKeys = new Set(snapshot.map((p) => p.key));
        pending.value = pending.value.filter((p) => !snapshotKeys.has(p.key));
      } catch (syncErr) {
        const msg = syncErr instanceof Error ? syncErr.message : String(syncErr);
        // Mark extra targets as error without rolling back successful ones
        syncTargets.value = syncTargets.value.map((st) =>
          st.selected && st.name !== t
            ? { ...st, status: "error" as SyncStatus, message: msg }
            : st,
        );
      } finally {
        syncSaving.value = false;
      }
    }
  } catch (err) {
    saveError.value = err instanceof ApiValidationError ? err.errors : [err instanceof Error ? err.message : String(err)];
  } finally {
    saving.value = false;
    if (again || editSeq !== seq) {
      again = false;
      markDirty();
    }
  }
}

function discard() {
  draft.value = server.value ? cloneDraft(server.value) : null;
  saveError.value = null;
  sel.value = null;
}

// ── toast + flash ───────────────────────────────────────────────────────────

let toastTimer: ReturnType<typeof setTimeout> | undefined;
function toast(msg: string) {
  toastMsg.value = msg;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (toastMsg.value = ""), 1800);
}

let flashTimer: ReturnType<typeof setTimeout> | undefined;
function flash(role: string) {
  flashRole.value = role;
  if (flashTimer) clearTimeout(flashTimer);
  flashTimer = setTimeout(() => (flashRole.value = null), 1200);
}

// ── pan / zoom / gestures (fix 5) ───────────────────────────────────────────

function onWheel(e: WheelEvent) {
  const r = canvasEl.value?.getBoundingClientRect();
  if (!r) return;
  Object.assign(view, wheelToView(view, e, e.clientX - r.left, e.clientY - r.top));
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
  const r = canvasEl.value?.getBoundingClientRect();
  if (!r) return;
  Object.assign(view, zoomAt(view, r.width / 2, r.height / 2, (startK * e.scale) / view.k));
  e.preventDefault();
}

// ── drag (blocks snap to the dot grid on release) ───────────────────────────

const dragging = ref<string | null>(null);
const snapped = ref<string | null>(null);
let suppressClick: string | null = null;

function onBlockDown(e: PointerEvent, key: string) {
  if (e.button !== 0) return;
  if (e.target instanceof Element && e.target.closest("button, input")) return;
  const pos = positions.value.get(key);
  if (!pos) return;
  const startX = e.clientX;
  const startY = e.clientY;
  const ox = pos.x;
  const oy = pos.y;
  let moved = false;
  const move = (ev: PointerEvent) => {
    const dx = (ev.clientX - startX) / view.k;
    const dy = (ev.clientY - startY) / view.k;
    if (!moved && Math.hypot(dx, dy) < 5) return;
    if (!moved) {
      moved = true;
      dragging.value = key;
    }
    pos.x = Math.max(0, ox + dx);
    pos.y = Math.max(0, oy + dy);
    redrawWires();
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    window.removeEventListener("pointercancel", up);
    if (!moved) return;
    dragging.value = null;
    pos.x = snap(pos.x);
    pos.y = snap(pos.y);
    snapped.value = key;
    setTimeout(() => {
      if (snapped.value === key) snapped.value = null;
    }, 180);
    suppressClick = key;
    redrawWires();
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
  window.addEventListener("pointercancel", up);
}

function onBlockSelect(role: string) {
  if (suppressClick === role) {
    suppressClick = null;
    return;
  }
  // an armed chip lands on the first block clicked
  if (armed.value) {
    const id = armed.value;
    armed.value = null;
    applyModel(role, id);
    return;
  }
  sel.value = role;
}

function onDefsClick(e: MouseEvent) {
  if (suppressClick === "__defs") {
    suppressClick = null;
    return;
  }
  openPop({ kind: "defaults" }, e.currentTarget as HTMLElement);
}

function onDropModel(role: string, id: string) {
  applyModel(role, id);
}

// ── wires ───────────────────────────────────────────────────────────────────

function setBlockEl(role: string, el: unknown) {
  const node = (el as { $el?: HTMLElement } | null)?.$el;
  if (node) blockEls.set(role, node);
  else blockEls.delete(role);
}

function redrawWires() {
  const d = draft.value;
  if (!d) {
    wirePaths.value = [];
    return;
  }
  wirePaths.value = chainPairs(benchRoles(d, selected.value)).map(([a, b]) => {
    const pa = positions.value.get(a) ?? { x: 0, y: 0 };
    const pb = positions.value.get(b) ?? { x: 0, y: 0 };
    const ha = blockEls.get(a)?.offsetHeight ?? BLOCK_H_EST;
    const hb = blockEls.get(b)?.offsetHeight ?? BLOCK_H_EST;
    return wirePath(
      { x: pa.x, y: pa.y, w: BLOCK_W, h: ha },
      { x: pb.x, y: pb.y, w: BLOCK_W, h: hb },
    );
  });
}

watch([draft, selected, positions, est], redrawWires, { flush: "post", deep: true });

// ── keyboard ────────────────────────────────────────────────────────────────

function escape(e: KeyEventLike): boolean {
  const t = e.target as HTMLElement | null;
  // Escape closes the popover first — even from inside its own search input
  if (pop.value) {
    closePop();
    return true;
  }
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) {
    // the proto blurs on Escape and lets the next Escape hit the shortcuts
    t.blur();
    return true;
  }
  if (dock.value !== "closed") {
    dock.value = "closed";
    return true;
  }
  if (armed.value) {
    armed.value = null;
    return true;
  }
  if (tray.value) {
    tray.value = false;
    return true;
  }
  if (sel.value) {
    sel.value = null;
    return true;
  }
  return false;
}

function onKey(e: KeyboardEvent) {
  if (!e.metaKey && !e.ctrlKey && !e.altKey && SHORTCUTS[e.key]) {
    const act = SHORTCUTS[e.key];
    if (act === "profiles") {
      dock.value = dock.value === "profiles" ? "closed" : "profiles";
      tray.value = false;
    } else if (act === "tray" || act === "search") {
      tray.value = act === "tray" ? !tray.value : true;
      dock.value = "closed";
    } else if (act === "add") {
      onFab();
    } else if (act === "new") {
      dock.value = "new-profile";
      tray.value = false;
    }
  } else if ((e.key === "Delete" || e.key === "Backspace") && sel.value) {
    e.preventDefault();
    const role = sel.value;
    edit((d) => removeRole(d, selected.value, role));
    sel.value = null;
  } else if ((e.key === "[" || e.key === "]") && sel.value) {
    stepRole(sel.value, e.key === "[" ? -1 : 1);
  } else if (sel.value && e.key.startsWith("Arrow")) {
    const pos = positions.value.get(sel.value);
    const next = pos ? nudge(pos, e.key, e.shiftKey) : null;
    if (next) {
      pos!.x = next.x;
      pos!.y = next.y;
      redrawWires();
    }
    e.preventDefault();
  }
}

useKeys(profileBindings((e) => onKey(e as KeyboardEvent)));

defineExpose({ escape });

function onDocMouseDown(e: MouseEvent) {
  const el = e.target as HTMLElement | null;
  // a click outside the popover closes it before anything else
  if (pop.value) {
    if (el && popEl.value && !popEl.value.contains(el) && popAnchor && !popAnchor.contains(el)) closePop();
    return;
  }
  if (dock.value === "closed") return;
  if (el && !el.closest(".dockwrap")) dock.value = "closed";
}

// ── popovers (one host — shared.js openPop contract) ──────────────────────

const POP_W = 340;

function openPop(kind: Pop, anchor: HTMLElement) {
  pop.value = kind;
  popAnchor = anchor;
  if (kind.kind === "rename-profile") renameIn.value = selected.value;
  void nextTick(() => {
    const h = popEl.value?.offsetHeight ?? 320;
    popPos.value = popPosition(anchor.getBoundingClientRect(), POP_W, h, window.innerWidth, window.innerHeight);
    if (kind.kind === "rename-profile") rnEl.value?.focus();
  });
}

function closePop() {
  pop.value = null;
  popAnchor = null;
}

function onRenameProfile() {
  const from = selected.value;
  const to = renameIn.value;
  // Re-key pending changes for this profile
  if (to && to !== from) {
    pending.value = pending.value.map((p) => {
      if (p.change.profile === from) {
        const newChange = { ...p.change, profile: to };
        const newKey = pendingChangesKey(to, p.change.role);
        return { key: newKey, change: newChange };
      }
      return p;
    });
  }
  let id = from;
  edit((d) => {
    id = renameProfile(d, from, to);
  });
  // positions follow the profile to its new name
  const L = layouts.get(from);
  if (L && id !== from) layouts.set(id, L);
  layouts.delete(from);
  selected.value = id;
  closePop();
}

function applyDefaultModel(id: string) {
  if (!draft.value) return;
  const change = defaultsModelChange(draft.value, selected.value, id, support.value);
  pending.value = coalesceChanges(pending.value, change);
  edit((d) => setDefaults(d, selected.value, change.patch as SlotPatch));
  toast(`defaults model → ${modelName(id)}`);
}

function applyDefaultThinking(delta: 1 | -1) {
  const cur = str(prof.value?.defaults?.thinking) ?? "medium";
  edit((d) => setDefaults(d, selected.value, { thinking: stepThinking(cur, delta) }));
}

function clearProfileDefaults() {
  edit((d) => clearDefaults(d, selected.value));
  closePop();
  toast("defaults cleared");
}

// the leg under an open model popover — for the note line and current marker
const popLeg = computed<Leg | null>(() => {
  const p = pop.value;
  if (!p || p.kind !== "model") return null;
  return est.value.legs.find((l) => l.role === p.role) ?? null;
});

/* ── sync target toggle ────────────────────────────────────────────────── */

function toggleTarget(name: string) {
  const t = target.value;
  if (!t) return;
  syncTargets.value = toggleSyncTarget(syncTargets.value, name, t);
  // Reset results when targets change
  syncResults.value = null;
}

// ── actions ─────────────────────────────────────────────────────────────────

function selectProfile(name: string) {
  selected.value = name;
  sel.value = null;
  dock.value = "closed";
}

function stepRole(role: string, delta: 1 | -1) {
  edit((d) =>
    setRole(d, selected.value, role, {
      thinking: stepThinking(
        resolveRole(d, defaults.value, selected.value, role, support.value).thinking,
        delta,
        levelsFor(support.value, resolveRole(d, defaults.value, selected.value, role, support.value).model),
      ),
    }),
  );
}

function onRenameRole(from: string, to: string) {
  const name = to.trim();
  // Re-key pending changes if role was renamed
  if (name && name !== from) {
    pending.value = rekeyPendingChange(pending.value, selected.value, from, name);
  } else if (!name) {
    pending.value = removePendingChange(pending.value, selected.value, from);
  }
  if (name && name !== from) {
    const L = layoutFor(selected.value);
    const pos = L.get(from);
    if (pos) {
      L.set(name, pos);
      L.delete(from);
    }
  }
  edit((d) => {
    if (!renameRole(d, selected.value, from, to) && !name) {
      // blank rename — renameRole refuses it, but the server owns the
      // rejection: put the blank name in the draft so PUT comes back with the
      // server's message (file untouched until it accepts)
      if (selected.value === "default") {
        const entry = agentByName(d, from);
        if (entry) entry.name = "";
      } else {
        const patch = d.profiles[selected.value]?.agents?.find(
          (a) => nameOf(a) === from,
        );
        if (patch) patch.name = "";
      }
    }
  });
  sel.value = name || from;
}

function onRemoveRole(role: string) {
  pending.value = removePendingChange(pending.value, selected.value, role);
  edit((d) => removeRole(d, selected.value, role));
  if (sel.value === role) sel.value = null;
}

function applyModel(role: string, id: string) {
  if (!draft.value) return;
  const change = modelChange(draft.value, defaults.value, selected.value, role, id, support.value);
  pending.value = coalesceChanges(pending.value, change);
  edit((d) => {
    setRole(d, selected.value, role, change.patch as SlotPatch);
    flash(role);
    toast(`${role} → ${modelName(id)}` + (change.patch.thinking !== undefined ? ` · thinking → ${change.patch.thinking}` : ""));
  });
}

function onPickModel(role: string, anchor: HTMLElement) {
  sel.value = role;
  openPop({ kind: "model", role }, anchor);
}

function applyPickedModel(role: string, id: string) {
  applyModel(role, id);
  closePop();
}

function onCreateProfile(payload: { name: string; copy: boolean }) {
  let id = "";
  edit((d) => {
    id = createProfile(d, payload.name, payload.copy ? selected.value : null);
  });
  if (id) {
    selected.value = id;
    dock.value = "closed";
  }
}

function onDeleteProfile(name: string) {
  // Remove pending changes for this profile
  pending.value = pending.value.filter((p) => p.change.profile !== name);
  edit((d) => deleteProfile(d, name));
  if (selected.value === name) selected.value = "default";
}

function onFab() {
  if (selected.value === "default") {
    toast("the roster already holds every role — add roles to a profile");
    return;
  }
  dock.value = "add-role";
}

function onChipClick(id: string) {
  if (sel.value) {
    applyModel(sel.value, id);
    return;
  }
  // no block selected → arm the chip; the next block click adopts it
  armed.value = armed.value === id ? null : id;
  if (armed.value) toast("armed — click a block to apply");
}

function pickRole(name: string) {
  addRole.value = name;
  dock.value = "add-model";
}

function onAddModel(id: string) {
  if (addRole.value) placeAgent(addRole.value, id);
}

// add flow, step 2 — the roster's chain order gives the new block its slot and
// its wires (placeMissing + chainPairs recompute), or swaps an on-bench model
function placeAgent(role: string, id: string) {
  if (!draft.value) return;
  const wasOnBench = benchRoles(draft.value, selected.value).includes(role);
  // Derive change from the untouched draft (no mutation yet)
  const change = modelChange(draft.value, defaults.value, selected.value, role, id, support.value);
  pending.value = coalesceChanges(pending.value, change);
  // Then mutate inside edit() so editSeq/markDirty/autosave run
  edit((d) => {
    setRole(d, selected.value, role, change.patch as SlotPatch);
  });
  addRole.value = null;
  dock.value = "closed";
  sel.value = role;
  flash(role);
  toast(`${role} ${wasOnBench ? "swapped to" : "joins the bench on"} ${modelName(id)}`);
}
</script>

<template>
  <div class="profiles-page">
    <div v-if="loadError" class="error-bar">profiles fetch failed — {{ loadError }}</div>
    <div v-else-if="!draft" class="empty faint">no registered project — add one to ~/.lime/projects</div>
    <template v-else>
      <header class="topbar">
        <span class="crumb mono">profiles</span>
        <span class="crumb-sep mono">/</span>
        <span class="crumb mono pname-proj">{{ target }}</span>
        <button
          v-if="selected !== 'default'"
          class="pname"
          title="click to rename"
          @click="openPop({ kind: 'rename-profile' }, $event.currentTarget as HTMLElement)"
        >{{ selected }}</button>
        <span v-else class="pname" title="the roster — edits save to agents:">{{ selected }}<small> · roster</small></span>
        <span class="chip count">{{ bench.length }} agents</span>
        <span class="tb-spacer" />
        <span
          class="chip est"
          :class="{ bad: est.broken && !est.empty }"
          title="Σ leg session cost ÷ P(all legs succeed)"
        >{{ est.empty ? "empty bench" : est.per === null ? "est —" : money(est.per) + " / task" }}</span>
        <span class="chip" title="P(every leg succeeds)">P ok <b>{{ est.empty ? "—" : Math.round(est.pAll * 100) + "%" }}</b></span>
        <span class="chip" title="Σ median session × thinking multiplier">wall <b>{{ est.broken ? "—" : fmtMs(est.wall) }}</b></span>
        <label
          class="pp-switch"
          :class="{ on: notionalOn }"
          title="price claude_code at API rate — same switch as the cost page"
        >
          <button
            class="pp-track"
            role="switch"
            :aria-checked="notionalOn"
            aria-label="price subscription at API rate"
            @click="notionalOn = !notionalOn"
          ><span class="pp-knob" /></button>
          <span class="pp-sw-label">sub at API rate</span>
        </label>
        <span v-if="saving" class="chip faint">saving…</span>
      </header>

      <div v-if="saveError" class="save-error">
        <b>not saved</b>
        <span v-for="m in saveError" :key="m" class="mono">{{ m }}</span>
        <button class="mini" @click="discard">discard edits</button>
      </div>

      <div
        ref="canvasEl"
        class="canvas"
        :style="{
          backgroundPosition: `${view.x}px ${view.y}px`,
          backgroundSize: `${GRID * view.k}px ${GRID * view.k}px`,
        }"
        @wheel.prevent="onWheel"
        @gesturestart.prevent="onGestureStart"
        @gesturechange.prevent="onGesture"
        @gestureend.prevent
        @pointerdown.self="sel = null"
      >
        <div
          class="stage"
          :style="{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})` }"
        >
          <svg class="wires" aria-hidden="true">
            <defs>
              <marker
                id="wireArr"
                viewBox="0 0 8 8"
                refX="7"
                refY="4"
                markerWidth="6"
                markerHeight="6"
                orient="auto-start-reverse"
              >
                <path d="M0 0 L8 4 L0 8 z" style="fill: var(--faint); stroke: none" />
              </marker>
            </defs>
            <path v-for="(d, i) in wirePaths" :key="i" :d="d" marker-end="url(#wireArr)" />
          </svg>

          <div
            v-if="hasDefs"
            class="blk defs"
            :style="{ left: posOf('__defs').x + 'px', top: posOf('__defs').y + 'px' }"
            title="defaults — feed blocks left on inherit"
            @pointerdown="onBlockDown($event, '__defs')"
            @click="onDefsClick"
          >
            <div class="blk-head">
              <span class="roledot" style="background: var(--border-strong)" />
              <b>defaults</b>
              <span class="chip src def">feeds inherit</span>
              <button
                class="blk-x"
                title="clear the defaults block"
                @click.stop="edit((d) => clearDefaults(d, selected))"
              >×</button>
            </div>
            <div class="defs-chips">
              <span class="chip rt" :class="{ cc: defsRt === 'claude_code' }">{{ defsRt }}</span>
              <span class="chip mono">{{ defsModel }}</span>
              <span class="chip">think {{ defsThink }}</span>
            </div>
          </div>

          <RoleBlock
            v-for="leg in est.legs"
            :key="leg.role"
            :ref="(el) => setBlockEl(leg.role, el)"
            :leg="leg"
            :pos="posOf(leg.role)"
            :selected="sel === leg.role"
            :dragging="dragging === leg.role"
            :snapped="snapped === leg.role"
            :flash="flashRole === leg.role"
            :color="roleColor(leg.role)"
            :cost="costLabel(leg, notionalOn)"
            :server-errors="errorsForRole(saveError, leg.role)"
            :want="!!armed"
            @pointerdown="onBlockDown($event, leg.role)"
            @select="onBlockSelect(leg.role)"
            @pick-model="onPickModel(leg.role, $event)"
            @step="stepRole(leg.role, $event)"
            @remove="onRemoveRole(leg.role)"
            @rename="onRenameRole(leg.role, $event)"
            @drop-model="onDropModel(leg.role, $event)"
          />

          <div v-if="est.empty && !hasDefs" class="empty-bench">
            <b>empty bench</b>
            <p>press <span class="kbd">＋</span> below to add an agent — or open <b>Models</b> and pick one.</p>
          </div>
        </div>

        <span class="canvas-note mono">{{ canvasNote(selected) }}</span>
        <span class="canvas-hint mono">⌫ remove · arrows nudge · [ ] thinking · scroll pans · pinch zooms</span>
      </div>

      <ProfilesDock
        :dock="dock"
        :tray="tray"
        :tray-q="trayQ"
        :selected="selected"
        :cards="cards"
        :models="trayList"
        :notional-on="notionalOn"
        :armed="armed"
        :add-role="addRole"
        :roster="roster"
        :sync-targets="syncTargets"
        :sync-results="syncResults"
        :sync-saving="syncSaving"
        :current-project="target ?? ''"
        @update:dock="dock = $event"
        @update:tray="tray = $event"
        @update:tray-q="trayQ = $event"
        @select="selectProfile"
        @delete="onDeleteProfile"
        @create="onCreateProfile"
        @fab="onFab"
        @chip-click="onChipClick"
        @pick-role="pickRole"
        @pick-model="onAddModel"
        @toggle-sync-target="toggleTarget"
      />

      <!-- the one popover host: model row · defaults · profile rename -->
      <div
        v-if="pop"
        ref="popEl"
        class="pp-pop"
        :style="{ left: popPos.left + 'px', top: popPos.top + 'px', width: POP_W + 'px' }"
      >
        <template v-if="pop.kind === 'model' && popLeg">
          <h5>{{ pop.role }} — pick a model</h5>
          <ModelPicker
            :models="trayList"
            :notional-on="notionalOn"
            :current="popLeg.model"
            autofocus
            @pick="applyPickedModel(pop.role, $event)"
          />
          <div class="pp-pop-row">
            <span class="pp-note mono">{{ popLeg.invalid ?? "alias → claude_code · provider/id → pi" }}</span>
          </div>
        </template>
        <template v-else-if="pop.kind === 'defaults'">
          <h5>{{ selected }} · defaults</h5>
          <ModelPicker
            :models="trayList"
            :notional-on="notionalOn"
            :current="str(prof?.defaults?.model)"
            autofocus
            @pick="applyDefaultModel"
          />
          <div class="pp-pop-row">
            <span class="step step-pop">
              <button aria-label="lower default thinking" @click="applyDefaultThinking(-1)">−</button>
              <button
                class="chip think"
                title="click to reset to inherit"
                @click="edit((d) => setDefaults(d, selected, { thinking: null }))"
              >think {{ defsThink }}</button>
              <button aria-label="raise default thinking" @click="applyDefaultThinking(1)">＋</button>
            </span>
            <span class="pop-spacer" />
            <button class="mini" @click="clearProfileDefaults">clear defaults</button>
          </div>
        </template>
        <template v-else>
          <h5>rename {{ selected }}</h5>
          <input
            ref="rnEl"
            v-model="renameIn"
            class="pp-search"
            aria-label="profile name"
            autocomplete="off"
            spellcheck="false"
            @keydown.enter.prevent="onRenameProfile"
          />
          <div class="pp-pop-row"><button class="mini lime" @click="onRenameProfile">rename</button></div>
        </template>
      </div>

      <div class="pp-toast mono" :class="{ show: !!toastMsg }">{{ toastMsg }}</div>
    </template>
  </div>
</template>

<style scoped>
.profiles-page {
  flex: 1 1 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  position: relative;
  overflow: hidden;
}

/* ── top bar ──────────────────────────────────────────────────────────────── */

.topbar {
  flex: none;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 16px;
  border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--bg) 88%, transparent);
}

.crumb {
  font-size: 12px;
  color: var(--faint);
  white-space: nowrap;
}

.crumb-sep {
  font-size: 12px;
  color: var(--faint);
  margin: 0 -3px;
}

/* ── popovers (one fixed host above the canvas) ───────────────────────── */

.pp-pop {
  position: fixed;
  z-index: 80;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: var(--shadow);
  padding: 12px;
  animation: pp-pop-in 0.14s ease-out;
}

@keyframes pp-pop-in {
  from {
    opacity: 0;
    transform: translateY(-4px);
  }
}

.pp-pop h5 {
  margin: 0 0 8px;
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.09em;
  color: var(--faint);
  font-weight: 500;
}

.pp-pop-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 10px;
}

.pop-spacer {
  flex: 1;
}

.pp-note {
  font-size: 10.5px;
  color: var(--faint);
}

.step-pop {
  font-size: 12px;
  color: var(--dim);
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

.pname {
  font-size: 15px;
  font-weight: 600;
  letter-spacing: -0.01em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.pname small {
  font-size: 12px;
  font-weight: 400;
  color: var(--faint);
}

.chip.count {
  color: var(--faint);
  font-size: 10px;
}

.chip b {
  color: var(--text);
}

.chip.est {
  border-color: var(--lime);
  color: var(--lime-ink);
  font-weight: 600;
}

.chip.est.bad {
  border-color: var(--bad);
  color: var(--bad);
}

.tb-spacer {
  flex: 1;
}

.pp-switch {
  display: flex;
  align-items: center;
  gap: 8px;
}

.pp-track {
  width: 34px;
  height: 20px;
  border-radius: 999px;
  background: var(--surface-2);
  border: 1px solid var(--border-strong);
  position: relative;
  flex: none;
  transition: background 0.2s;
}

.pp-switch.on .pp-track {
  background: var(--lime);
  border-color: var(--lime);
}

.pp-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--surface);
  box-shadow: var(--shadow);
  transition: left 0.2s;
  display: block;
}

.pp-switch.on .pp-knob {
  left: 16px;
}

.pp-sw-label {
  font-size: 11.5px;
  color: var(--dim);
  white-space: nowrap;
}

.save-error {
  margin: 10px 16px 0;
  padding: 8px 12px;
  border: 1px solid var(--bad);
  background: var(--bad-chip);
  border-radius: 10px;
  font-size: 12px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  align-items: center;
  color: var(--bad);
}

.save-error b {
  color: var(--bad);
}

.save-error .mono {
  min-width: 0;
}

/* ── dotted canvas ────────────────────────────────────────────────────────── */

.canvas {
  flex: 1;
  min-height: 0;
  position: relative;
  overflow: hidden;
  overscroll-behavior: contain;
  touch-action: none;
  background-image: radial-gradient(var(--dot) 1.15px, transparent 1.15px);
}

.stage {
  position: absolute;
  inset: 0;
  transform-origin: 0 0;
}

.wires {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  overflow: visible;
  z-index: 0;
}

.wires path {
  fill: none;
  stroke: var(--border-strong);
  stroke-width: 1.5;
  stroke-dasharray: 5 5;
}

.canvas-hint {
  position: absolute;
  top: 10px;
  right: 14px;
  font-size: 10px;
  color: var(--faint);
  pointer-events: none;
  z-index: 1;
}

.canvas-note {
  position: absolute;
  left: 14px;
  top: 10px;
  font-size: 10.5px;
  color: var(--dim);
  border: 1px dashed var(--border-strong);
  border-radius: 999px;
  padding: 3px 11px;
  background: color-mix(in srgb, var(--surface) 70%, transparent);
  z-index: 1;
  pointer-events: none;
}

/* the defaults block lives in this component — RoleBlock's scoped .blk rules
   don't reach it, so the shared anatomy is repeated here */
.blk.defs {
  position: absolute;
  z-index: 1;
  width: 236px;
  border: 1px dashed var(--border);
  border-radius: 12px;
  box-shadow: var(--shadow);
  padding: 10px 12px;
  background: color-mix(in srgb, var(--surface) 72%, transparent);
  cursor: grab;
  user-select: none;
  touch-action: none;
}

.blk.defs .blk-head {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 9px;
  font-size: 13px;
}

.blk.defs .blk-head b {
  font-size: 13.5px;
}

.blk.defs .roledot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  display: inline-block;
  flex: none;
}

.blk.defs .chip.src {
  color: var(--faint);
  font-size: 10px;
  padding: 1px 7px;
}

.blk.defs .chip.src.def {
  border-style: dashed;
}

.blk.defs .blk-x {
  margin-left: auto;
  color: var(--faint);
  font-size: 15px;
  line-height: 1;
  padding: 0 2px;
}

.blk.defs .blk-x:hover {
  color: var(--bad);
}

.defs-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.empty-bench {
  position: absolute;
  left: 50%;
  top: 40%;
  transform: translate(-50%, -50%);
  text-align: center;
  color: var(--faint);
  border: 1.5px dashed var(--border-strong);
  border-radius: 14px;
  padding: 30px 42px;
  background: color-mix(in srgb, var(--surface) 55%, transparent);
  max-width: 380px;
}

.empty-bench b {
  display: block;
  font-size: 15px;
  color: var(--dim);
  margin: 8px 0 4px;
}

.empty-bench p {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.7;
}

.kbd {
  border: 1px solid var(--border-strong);
  border-radius: 5px;
  padding: 0 5px;
  font-family: var(--mono);
  font-size: 11px;
  background: var(--surface);
}

.empty {
  padding: 60px 24px;
  text-align: center;
}

/* ── toast ────────────────────────────────────────────────────────────────── */

.pp-toast {
  position: absolute;
  bottom: 84px;
  left: 50%;
  transform: translate(-50%, 8px);
  background: var(--ink);
  color: var(--bg);
  font-size: 11.5px;
  padding: 7px 14px;
  border-radius: 999px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.2s, transform 0.2s;
  z-index: 99;
  white-space: nowrap;
}

.pp-toast.show {
  opacity: 1;
  transform: translate(-50%, 0);
}
</style>