<script setup lang="ts">
// The left-pane file tree: a lazy directory listing with live name search, a
// project switch that re-keys the whole tree and one inline error line. Ported
// from prototype B's FileTree/TreeChildren (proto-workspace/b/app.js).
import { computed, nextTick, ref, shallowRef, watch } from 'vue'
import type { ComponentPublicInstance } from 'vue'
import type { FileEntry, FileKind } from '../lib/types'
import { fetchDir, fetchFileIndex } from '../lib/api'
import { ancestorsOf, buildSearchIndex, searchFiles } from '../lib/fileSearch'
import type { SearchIndex, SearchResult } from '../lib/fileSearch'
import { fileIconSvg, fileKind, glyph } from '../lib/fileIcons'
import { submitCreate, submitRename, submitTrash, trashConfirmText } from '../lib/fileTree'
import { isUnder, movedPath } from '../lib/editorTabs'

const props = defineProps<{
  project: string
  active: string | null
  dirtyUnder?: (path: string) => string[]
}>()
const emit = defineEmits<{
  open: [path: string]
  renamed: [from: string, to: string]
  trashed: [path: string]
}>()

// ── State ────────────────────────────────────────────────────────────────────
const listings = ref<Record<string, FileEntry[]>>({})
const expanded = ref(new Set<string>())
const loadError = ref<string | null>(null)
const index = shallowRef<SearchIndex | null>(null)
const query = ref('')
const sel = ref(0)
const treeEl = ref<HTMLElement | null>(null)
let gen = 0

// ── Inline create ────────────────────────────────────────────────────────────
const creating = ref<{ parent: string; kind: FileKind } | null>(null)
const addMenu = ref<{ parent: string; left: number; top: number } | null>(null)
const name = ref('')
const err = ref('')
const createInput = ref<HTMLInputElement | null>(null)
let busy = false

// ── Row menu, inline rename and the Trash confirm ────────────────────────────
const rowMenu = ref<{ entry: FileEntry; left: number; top: number } | null>(null)
const renaming = ref<{ path: string; parent: string } | null>(null)
const renameName = ref('')
const renameErr = ref('')
const renameInput = ref<HTMLInputElement | null>(null)
const trashAsk = ref<{ entry: FileEntry; dirty: string[]; error: string } | null>(null)
const trashBtn = ref<HTMLButtonElement | null>(null)
const trashText = computed(() =>
  trashAsk.value ? trashConfirmText(trashAsk.value.entry.name, trashAsk.value.dirty) : null,
)

// ── Loads, all guarded by the generation counter ─────────────────────────────
async function loadDir(dir: string) {
  const g = gen
  try {
    const res = await fetchDir(props.project, dir)
    if (g !== gen) return
    listings.value = { ...listings.value, [dir]: res.entries }
  } catch (e) {
    if (g !== gen) return
    loadError.value ??= `Couldn't load ${dir || 'files'}: ${(e as Error).message}`
    const next = new Set(expanded.value)
    next.delete(dir)
    expanded.value = next
  }
}

async function loadIndex() {
  const g = gen
  try {
    const res = await fetchFileIndex(props.project)
    if (g !== gen) return
    index.value = buildSearchIndex(res.paths)
  } catch (e) {
    if (g !== gen) return
    loadError.value ??= `Couldn't load the search index: ${(e as Error).message}`
  }
}

function reset() {
  gen++
  listings.value = {}
  expanded.value = new Set()
  loadError.value = null
  index.value = null
  query.value = ''
  sel.value = 0
  creating.value = null
  addMenu.value = null
  rowMenu.value = null
  renaming.value = null
  renameName.value = ''
  renameErr.value = ''
  trashAsk.value = null
  busy = false
  void loadDir('')
  void loadIndex()
}
watch(() => props.project, reset, { immediate: true })

// ── Tree rows: a flattened walk of the loaded listings, no recursion ─────────
interface TreeRow {
  key: string
  depth: number
  entry: FileEntry | null
  create?: boolean
}

const rows = computed<TreeRow[]>(() => {
  const out: TreeRow[] = []
  const walk = (dir: string, depth: number) => {
    if (creating.value && creating.value.parent === dir) {
      out.push({ key: `create:${dir}`, depth, entry: null, create: true })
    }
    const entries = listings.value[dir]
    if (!entries) {
      if (dir) out.push({ key: `loading:${dir}`, depth, entry: null })
      return
    }
    for (const entry of entries) {
      out.push({ key: entry.path, depth, entry })
      if (entry.dir && expanded.value.has(entry.path)) walk(entry.path, depth + 1)
    }
  }
  walk('', 0)
  return out
})

const rootEmpty = computed(
  () => !!listings.value[''] && listings.value[''].length === 0 && !loadError.value,
)

function isOpen(path: string) {
  return expanded.value.has(path)
}

function toggleDir(entry: FileEntry) {
  const next = new Set(expanded.value)
  if (next.has(entry.path)) {
    next.delete(entry.path)
  } else {
    next.add(entry.path)
    if (!listings.value[entry.path]) void loadDir(entry.path)
  }
  expanded.value = next
}

function onRow(entry: FileEntry) {
  if (entry.dir) toggleDir(entry)
  else emit('open', entry.path)
}

// ── Search ───────────────────────────────────────────────────────────────────
const result = computed<SearchResult | null>(() =>
  index.value && query.value.trim() ? searchFiles(index.value, query.value) : null,
)
const hits = computed(() => result.value?.hits ?? [])

function moveSel(delta: number) {
  const n = hits.value.length
  if (!n) return
  sel.value = Math.min(n - 1, Math.max(0, sel.value + delta))
}

function openSel() {
  const hit = hits.value[sel.value]
  if (hit) void openHit(hit.path)
}

function clearSearch() {
  query.value = ''
  sel.value = 0
}

function onEsc(ev: KeyboardEvent) {
  if (query.value) clearSearch()
  else (ev.target as HTMLInputElement | null)?.blur()
}

async function openHit(path: string) {
  clearSearch()
  emit('open', path)
  await reveal(path)
}

/** Expand the path's ancestors, load any unlisted dir, then scroll it into view. */
async function reveal(path: string) {
  const g = gen
  const next = new Set(expanded.value)
  const toLoad: string[] = []
  for (const a of ancestorsOf(path)) {
    next.add(a)
    if (!listings.value[a]) toLoad.push(a)
  }
  expanded.value = next
  await Promise.all(toLoad.map((dir) => loadDir(dir)))
  if (g !== gen) return
  await nextTick()
  treeEl.value
    ?.querySelector(`[data-path="${CSS.escape(path)}"]`)
    ?.scrollIntoView({ block: 'nearest' })
}

// ── Create: the add menu, the inline row and its commit/cancel ───────────────
function openAdd(parent: string, ev: MouseEvent) {
  const el = ev.currentTarget as HTMLElement | null
  if (!el) return
  const rect = el.getBoundingClientRect()
  addMenu.value = {
    parent,
    left: Math.max(8, Math.min(rect.left, window.innerWidth - 218)),
    top: rect.bottom + 6,
  }
}

function setCreateInput(el: Element | ComponentPublicInstance | null) {
  createInput.value = el as HTMLInputElement | null
}

async function startCreate(kind: FileKind) {
  const menu = addMenu.value
  addMenu.value = null
  if (!menu) return
  const parent = menu.parent
  if (parent) {
    const next = new Set(expanded.value)
    next.add(parent)
    expanded.value = next
    if (!listings.value[parent]) await loadDir(parent)
  }
  creating.value = { parent, kind }
  name.value = ''
  err.value = ''
  await nextTick()
  createInput.value?.focus()
}

function cancel() {
  creating.value = null
  err.value = ''
}

function shake() {
  const el = createInput.value
  if (!el) return
  el.classList.remove('shake')
  void el.offsetWidth
  el.classList.add('shake')
}

async function commit() {
  if (busy) return
  const c = creating.value
  if (!c) return
  busy = true
  const g = gen
  const res = await submitCreate({
    project: props.project,
    parent: c.parent,
    raw: name.value,
    kind: c.kind,
    entries: listings.value[c.parent],
  })
  busy = false
  if (g !== gen || creating.value !== c) return
  if ('error' in res) {
    err.value = res.error
    shake()
    return
  }
  creating.value = null
  err.value = ''
  await loadDir(c.parent)
  const next = new Set(expanded.value)
  for (const a of ancestorsOf(res.created.path)) next.add(a)
  if (res.created.kind === 'dir') next.add(res.created.path)
  expanded.value = next
  const toLoad = [...next].filter((d) => d && !listings.value[d])
  await Promise.all(toLoad.map((d) => loadDir(d)))
  void loadIndex()
  if (res.created.kind === 'file') emit('open', res.created.path)
  await reveal(res.created.path)
}

function onBlur() {
  setTimeout(() => {
    if (busy || !creating.value) return
    if (!name.value.trim() || err.value) {
      cancel()
      return
    }
    void commit()
  }, 0)
}

// ── Row menu: right-click / F2 rename / ⌘⌫ Trash ─────────────────────────────
function setRenameInput(el: Element | ComponentPublicInstance | null) {
  renameInput.value = el as HTMLInputElement | null
}

function cancelRename() {
  renaming.value = null
  renameErr.value = ''
}

function shakeRename() {
  const el = renameInput.value
  if (!el) return
  el.classList.remove('shake')
  void el.offsetWidth
  el.classList.add('shake')
}

function openRowMenu(entry: FileEntry, ev: MouseEvent) {
  addMenu.value = null
  if (creating.value) cancel()
  if (renaming.value) cancelRename()
  rowMenu.value = {
    entry,
    left: Math.max(8, Math.min(ev.clientX, window.innerWidth - 218)),
    top: Math.max(8, Math.min(ev.clientY, window.innerHeight - 100)),
  }
}

function onRowKey(entry: FileEntry, ev: KeyboardEvent) {
  if (ev.target !== ev.currentTarget) return
  if (ev.key === 'F2') {
    ev.preventDefault()
    void startRename(entry)
    return
  }
  if (ev.key === 'Delete' || (ev.key === 'Backspace' && ev.metaKey)) {
    ev.preventDefault()
    void askTrash(entry)
  }
}

async function startRename(entry: FileEntry) {
  rowMenu.value = null
  addMenu.value = null
  if (creating.value) cancel()
  const cut = entry.path.lastIndexOf('/')
  renaming.value = {
    path: entry.path,
    parent: cut < 0 ? '' : entry.path.slice(0, cut),
  }
  renameName.value = entry.name
  renameErr.value = ''
  await nextTick()
  const el = renameInput.value
  if (!el) return
  el.focus()
  const dot = entry.dir ? -1 : entry.name.lastIndexOf('.')
  if (dot > 0) el.setSelectionRange(0, dot)
  else el.select()
}

async function commitRename() {
  if (busy) return
  const r = renaming.value
  if (!r) return
  busy = true
  const g = gen
  const res = await submitRename({
    project: props.project,
    from: r.path,
    raw: renameName.value,
    entries: listings.value[r.parent],
  })
  busy = false
  if (g !== gen || renaming.value !== r) return
  if ('error' in res) {
    renameErr.value = res.error
    shakeRename()
    return
  }
  if (res.renamed === null) {
    cancelRename()
    return
  }
  const from = r.path
  const to = res.renamed.to
  renaming.value = null
  renameErr.value = ''
  const nextListings: Record<string, FileEntry[]> = {}
  for (const [key, entries] of Object.entries(listings.value)) {
    if (!isUnder(key, from)) nextListings[key] = entries
  }
  listings.value = nextListings
  const nextExpanded = new Set<string>()
  for (const dir of expanded.value) nextExpanded.add(movedPath(dir, from, to))
  expanded.value = nextExpanded
  emit('renamed', from, to)
  await loadDir(r.parent)
  const toLoad = [...nextExpanded].filter((dir) => dir && !listings.value[dir])
  await Promise.all(toLoad.map((dir) => loadDir(dir)))
  void loadIndex()
  await reveal(to)
  const row = treeEl.value?.querySelector(`[data-path="${CSS.escape(to)}"]`)
  if (row instanceof HTMLElement) row.focus()
}

function onRenameBlur() {
  setTimeout(() => {
    if (busy || !renaming.value) return
    if (!renameName.value.trim() || renameErr.value) {
      cancelRename()
      return
    }
    void commitRename()
  }, 0)
}

async function askTrash(entry: FileEntry) {
  rowMenu.value = null
  addMenu.value = null
  trashAsk.value = {
    entry,
    dirty: props.dirtyUnder?.(entry.path) ?? [],
    error: '',
  }
  await nextTick()
  trashBtn.value?.focus()
}

async function confirmTrash() {
  if (busy) return
  const ask = trashAsk.value
  if (!ask) return
  busy = true
  const g = gen
  const res = await submitTrash(props.project, ask.entry.path)
  busy = false
  if (g !== gen || trashAsk.value !== ask) return
  if ('error' in res) {
    trashAsk.value.error = res.error
    return
  }
  const path = ask.entry.path
  const cut = path.lastIndexOf('/')
  const parent = cut < 0 ? '' : path.slice(0, cut)
  trashAsk.value = null
  const nextListings: Record<string, FileEntry[]> = {}
  for (const [key, entries] of Object.entries(listings.value)) {
    if (!isUnder(key, path)) nextListings[key] = entries
  }
  listings.value = nextListings
  const nextExpanded = new Set<string>()
  for (const dir of expanded.value) {
    if (!isUnder(dir, path)) nextExpanded.add(dir)
  }
  expanded.value = nextExpanded
  emit('trashed', path)
  await loadDir(parent)
  void loadIndex()
}

function escape(): boolean {
  if (rowMenu.value) {
    rowMenu.value = null
    return true
  }
  if (trashAsk.value && !busy) {
    trashAsk.value = null
    return true
  }
  return false
}

defineExpose({ escape })
</script>

<template>
  <div class="tree-pane">
    <div class="pane-head">
      <span class="pane-title">Files</span>
      <button
        class="icon-btn add-root"
        title="New file or folder"
        @click.stop="openAdd('', $event)"
        v-html="glyph('plus', 15)"
      />
    </div>

    <div class="search">
      <span class="search-ic" v-html="glyph('search', 14)" />
      <input
        class="search-input"
        v-model="query"
        placeholder="Search files"
        spellcheck="false"
        autocomplete="off"
        @input="sel = 0"
        @keydown.down.prevent="moveSel(1)"
        @keydown.up.prevent="moveSel(-1)"
        @keydown.enter.prevent="openSel"
        @keydown.esc.prevent="onEsc"
      />
      <button
        v-if="query"
        class="search-clear"
        title="Clear search"
        @click="clearSearch"
        v-html="glyph('x', 13)"
      />
    </div>

    <button v-if="loadError" class="load-err" :title="loadError" @click="reset">
      <span class="load-err-ic" v-html="glyph('alert', 14)" />
      <span class="load-err-text">{{ loadError }}</span>
    </button>

    <div v-if="query.trim()" class="hits">
      <template v-if="result">
        <div
          v-for="(hit, i) in result.hits"
          :key="hit.path"
          class="hit"
          :class="{ on: i === sel }"
          @mouseenter="sel = i"
          @click="openHit(hit.path)"
        >
          <span class="ficon" :class="fileKind(hit.path).tint" v-html="fileIconSvg(hit.path)" />
          <span class="hit-name">
            <template v-if="hit.at >= 0">
              <span>{{ hit.name.slice(0, hit.at) }}</span
              ><mark>{{ hit.name.slice(hit.at, hit.at + hit.len) }}</mark
              ><span>{{ hit.name.slice(hit.at + hit.len) }}</span>
            </template>
            <template v-else>{{ hit.name }}</template>
          </span>
          <span v-if="hit.dir" class="hit-dir">{{ hit.dir }}</span>
        </div>
        <div v-if="!result.hits.length" class="empty-line">No matching files</div>
        <div v-if="result.total > result.hits.length" class="hits-foot">
          Showing {{ result.hits.length }} of {{ result.total }}
        </div>
      </template>
      <div v-else-if="!loadError" class="empty-line">Indexing…</div>
    </div>

    <div v-else ref="treeEl" class="tree">
      <template v-for="row in rows" :key="row.key">
        <div
          v-if="row.create"
          class="row creating"
          :style="{ paddingLeft: 8 + row.depth * 14 + 'px' }"
        >
          <span class="chev" />
          <span
            class="ficon"
            :class="creating && creating.kind === 'dir' ? 'ft-dir' : fileKind(name || 'x.txt').tint"
            v-html="
              creating && creating.kind === 'dir'
                ? glyph('folder', 15)
                : fileIconSvg(name || 'x.txt')
            "
          />
          <input
            :ref="setCreateInput"
            class="create-input"
            :class="{ bad: !!err }"
            v-model="name"
            :placeholder="creating && creating.kind === 'dir' ? 'New folder' : 'New file'"
            spellcheck="false"
            autocomplete="off"
            @keydown.enter.prevent="commit"
            @keydown.esc.prevent.stop="cancel"
            @input="err = ''"
            @blur="onBlur"
          />
          <span v-if="err" class="create-err">{{ err }}</span>
        </div>
        <div
          v-else-if="row.entry"
          class="row"
          :class="{
            dir: row.entry.dir,
            open: row.entry.dir && isOpen(row.entry.path),
            active: !row.entry.dir && row.entry.path === active,
          }"
          :style="{ paddingLeft: 8 + row.depth * 14 + 'px' }"
          :data-path="row.entry.path"
          tabindex="-1"
          @click="onRow(row.entry)"
          @contextmenu.prevent="openRowMenu(row.entry, $event)"
          @keydown="onRowKey(row.entry, $event)"
        >
          <span class="chev" v-if="row.entry.dir" v-html="glyph('chevronRight', 13, 2)" />
          <span class="chev" v-else />
          <span
            class="ficon"
            :class="row.entry.dir ? 'ft-dir' : fileKind(row.entry.name).tint"
            v-html="
              row.entry.dir
                ? glyph(isOpen(row.entry.path) ? 'folderOpen' : 'folder', 15)
                : fileIconSvg(row.entry.name)
            "
          />
          <template v-if="renaming?.path === row.entry.path">
            <input
              :ref="setRenameInput"
              class="create-input"
              :class="{ bad: !!renameErr }"
              v-model="renameName"
              spellcheck="false"
              autocomplete="off"
              @click.stop
              @keydown.enter.prevent="commitRename"
              @keydown.esc.prevent.stop="cancelRename"
              @input="renameErr = ''"
              @blur="onRenameBlur"
            />
            <span v-if="renameErr" class="create-err">{{ renameErr }}</span>
          </template>
          <span v-else class="name">{{ row.entry.name }}</span>
          <button
            v-if="row.entry.dir"
            class="row-add"
            title="New file or folder"
            @click.stop="openAdd(row.entry.path, $event)"
            v-html="glyph('plus', 14)"
          />
        </div>
        <div v-else class="row muted" :style="{ paddingLeft: 8 + row.depth * 14 + 'px' }">
          <span class="chev" />
          <span class="name faint">Loading…</span>
        </div>
      </template>
      <div v-if="rootEmpty" class="empty-line">Empty folder</div>
    </div>

    <div v-if="addMenu" class="catcher" @mousedown="addMenu = null" />
    <div
      v-if="addMenu"
      class="pop menu"
      :style="{ left: addMenu.left + 'px', top: addMenu.top + 'px' }"
    >
      <div v-if="addMenu.parent" class="menu-cap">{{ addMenu.parent }}/</div>
      <button class="menu-item" @click="startCreate('file')">
        <span v-html="glyph('filePlus', 15)" /><span class="menu-label">New file</span>
      </button>
      <button class="menu-item" @click="startCreate('dir')">
        <span v-html="glyph('folderPlus', 15)" /><span class="menu-label">New folder</span>
      </button>
    </div>

    <div v-if="rowMenu" class="catcher" @mousedown="rowMenu = null" />
    <div
      v-if="rowMenu"
      class="pop menu"
      :style="{ left: rowMenu.left + 'px', top: rowMenu.top + 'px' }"
    >
      <div class="menu-cap">{{ rowMenu.entry.path }}</div>
      <button class="menu-item" @click="startRename(rowMenu.entry)">
        <span v-html="glyph('pencil', 15)" /><span class="menu-label">Rename</span>
        <span class="menu-hint">F2</span>
      </button>
      <button class="menu-item" @click="askTrash(rowMenu.entry)">
        <span v-html="glyph('trash', 15)" /><span class="menu-label">Move to Trash</span>
        <span class="menu-hint">⌘⌫</span>
      </button>
    </div>

    <Teleport to="body">
      <div v-if="trashAsk" class="confirm-layer">
        <div class="confirm-scrim" @click="trashAsk = null" />
        <div class="confirm" role="alertdialog">
          <div class="confirm-t">{{ trashText?.title }}</div>
          <div class="confirm-s">{{ trashText?.detail }}</div>
          <div v-if="trashAsk.error" class="confirm-err">{{ trashAsk.error }}</div>
          <div class="pop-actions">
            <button class="btn ghost" @click="trashAsk = null">Cancel</button>
            <button ref="trashBtn" class="btn primary" @click="confirmTrash">
              Move to Trash
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.tree-pane {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  --ft-md: #64748b;
  --ft-py: #3b82f6;
  --ft-ts: #2f74c0;
  --ft-js: #ca8a04;
  --ft-vue: #16a34a;
  --ft-yaml: #a855f7;
  --ft-data: #d97706;
  --ft-img: #db2777;
  --ft-html: #ea580c;
  --ft-plain: #8a8a84;
  --ft-dir: #8a8a84;
  --ft-css: #2563eb;
  --ft-sh: #4d7c0f;
  --ft-sql: #0e7490;
  --ft-docker: #0284c7;
  --ft-git: #ea580c;
  --ft-npm: #dc2626;
  --ft-claude: #d97757;
  --ft-toml: #9a3412;
}
.pane-head {
  height: 46px;
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 10px 0 16px;
}
.pane-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
  letter-spacing: 0.01em;
}
.icon-btn {
  width: 26px;
  height: 26px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 7px;
  color: var(--muted);
  transition: background 0.12s, color 0.12s, opacity 0.12s;
}
.icon-btn:hover {
  background: var(--hover);
  color: var(--text);
}
.add-root {
  opacity: 0.55;
}
.tree-pane:hover .add-root {
  opacity: 1;
}

.row-add {
  width: 22px;
  height: 22px;
  border-radius: 6px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--muted);
  opacity: 0;
  transition: opacity 0.1s, background 0.1s;
  flex: none;
  margin-right: -2px;
}
.row.dir:hover .row-add,
.row-add:focus-visible {
  opacity: 1;
}
.row-add:hover {
  background: var(--press);
  color: var(--text);
}
.row.creating {
  background: var(--hover);
}
.create-input {
  flex: 1;
  min-width: 0;
  height: 22px;
  border: 1px solid var(--lime);
  border-radius: 5px;
  padding: 0 6px;
  outline: none;
  font: inherit;
  color: var(--text);
  background: var(--panel);
}
.create-input.bad {
  border-color: var(--bad);
}
.create-input.shake {
  animation: shake 0.26s var(--ease);
}
.create-err {
  position: absolute;
  right: 12px;
  top: 28px;
  z-index: 3;
  font-size: 11.5px;
  color: var(--bad);
  background: var(--raised);
  padding: 2px 7px;
  border-radius: 5px;
  box-shadow: var(--shadow-pop);
}
@keyframes shake {
  20% {
    transform: translateX(-3px);
  }
  60% {
    transform: translateX(3px);
  }
  100% {
    transform: none;
  }
}
.catcher {
  position: fixed;
  inset: 0;
  z-index: 55;
}
.pop {
  position: fixed;
  z-index: 60;
  background: var(--raised);
  border-radius: 12px;
  box-shadow: var(--shadow-pop);
  padding: 6px;
}
.menu {
  min-width: 200px;
}
.menu-cap {
  font-size: 11px;
  color: var(--faint);
  padding: 4px 10px 6px;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.menu-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 9px;
  height: 32px;
  padding: 0 10px;
  border-radius: 7px;
  color: var(--text);
  transition: background 0.08s;
}
.menu-item:hover {
  background: var(--hover);
}
.menu-item > span:first-child {
  color: var(--muted);
}
.menu-label {
  flex: 1;
  text-align: left;
  color: var(--text);
}
.menu-hint {
  flex: none;
  font-size: 11px;
  color: var(--faint);
  letter-spacing: 0.02em;
}

.search {
  flex: none;
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 10px 6px 12px;
  padding: 0 8px;
  height: 30px;
  border-radius: 7px;
  background: var(--panel-2);
  border: 1px solid var(--line);
}
.search-ic {
  display: inline-flex;
  color: var(--faint);
}
.search-input {
  flex: 1;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  font: inherit;
  font-size: 12.5px;
  color: var(--text);
}
.search-clear {
  width: 20px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 5px;
  color: var(--muted);
}
.search-clear:hover {
  background: var(--hover);
  color: var(--text);
}

.load-err {
  flex: none;
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 10px 6px 12px;
  padding: 5px 8px;
  border-radius: 7px;
  color: var(--bad);
  font-size: 12px;
  text-align: left;
  white-space: nowrap;
  overflow: hidden;
}
.load-err:hover {
  background: var(--hover);
}
.load-err-ic {
  display: inline-flex;
  flex: none;
}
.load-err-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tree,
.hits {
  flex: 1;
  overflow: auto;
  min-height: 0;
  padding: 0 6px 24px;
  scrollbar-width: thin;
  scrollbar-color: var(--line) transparent;
}
.row {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding-right: 6px;
  border-radius: 7px;
  color: var(--text);
  cursor: default;
  user-select: none;
  white-space: nowrap;
  transition: background 0.1s;
}
.row:hover {
  background: var(--hover);
}
.row:focus {
  outline: none;
  background: var(--hover);
}
.row.active {
  background: color-mix(in srgb, var(--lime) 18%, transparent);
  font-weight: 520;
}
.row.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 7px;
  bottom: 7px;
  width: 2px;
  border-radius: 2px;
  background: var(--lime-ink);
}
.chev {
  width: 13px;
  display: inline-flex;
  flex: none;
  color: var(--faint);
  transition: transform 0.14s var(--ease);
}
.row.open > .chev {
  transform: rotate(90deg);
}
.ficon {
  display: inline-flex;
  flex: none;
  color: var(--ft-plain);
  opacity: 0.9;
}
.row.dir .ficon {
  color: var(--ft-dir);
}
.name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
.name.faint,
.muted .name {
  color: var(--faint);
}
.brand-ic {
  display: block;
}

.hit {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 8px;
  border-radius: 7px;
  cursor: default;
  user-select: none;
  white-space: nowrap;
}
.hit:hover,
.hit.on {
  background: var(--hover);
}
.hit-name {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--text);
}
.hit-name mark {
  background: transparent;
  color: var(--lime-ink);
  font-weight: 600;
}
.hit-dir {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--faint);
  font-size: 12px;
}
.hits-foot {
  padding: 6px 10px;
  font-size: 11.5px;
  color: var(--faint);
}
.empty-line {
  padding: 10px 12px;
  font-size: 12.5px;
  color: var(--faint);
}

/* ── confirm dialog (teleported under <body>, outside .ws) ────────────────── */
.confirm-layer {
  position: fixed;
  inset: 0;
  z-index: 80;
  display: flex;
  align-items: center;
  justify-content: center;
}
.confirm-scrim {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.32);
  backdrop-filter: blur(2px);
}
.confirm {
  position: relative;
  width: 380px;
  padding: 20px;
  background: var(--surface);
  color: var(--text);
  border-radius: 16px;
  box-shadow: var(--shadow);
}
.confirm-t {
  font-weight: 640;
  font-size: 14.5px;
  margin-bottom: 4px;
  overflow-wrap: anywhere;
}
.confirm-s {
  color: var(--dim);
  font-size: 13px;
  margin-bottom: 18px;
}
.confirm-err {
  margin: -10px 0 14px;
  font-size: 12.5px;
  color: var(--bad);
  overflow-wrap: anywhere;
}
.pop-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 30px;
  padding: 0 12px;
  border-radius: 8px;
  font-family: var(--sans);
  font-weight: 540;
  font-size: 13px;
  transition: background 0.12s, color 0.12s, opacity 0.12s;
}
/* Ring drawn inside the 30px box, so a focused button keeps its size and line. */
.btn:focus-visible {
  outline: 2px solid var(--purple);
  outline-offset: -2px;
}
.btn.ghost {
  color: var(--dim);
  box-shadow: inset 0 0 0 1px var(--border);
}
.btn.ghost:hover {
  background: color-mix(in srgb, var(--text) 6%, transparent);
  color: var(--text);
}
.btn.primary {
  background: var(--lime);
  /* text on a lime fill uses --ink, never --lime-ink, which is lime in dark */
  color: var(--ink);
  font-weight: 620;
}
.btn.primary:hover {
  filter: brightness(1.05);
}

.ft-md {
  color: var(--ft-md);
}
.ft-py {
  color: var(--ft-py);
}
.ft-ts {
  color: var(--ft-ts);
}
.ft-js {
  color: var(--ft-js);
}
.ft-vue {
  color: var(--ft-vue);
}
.ft-yaml {
  color: var(--ft-yaml);
}
.ft-data {
  color: var(--ft-data);
}
.ft-img {
  color: var(--ft-img);
}
.ft-html {
  color: var(--ft-html);
}
.ft-plain {
  color: var(--ft-plain);
}
.ft-dir {
  color: var(--ft-dir);
}
.ft-css {
  color: var(--ft-css);
}
.ft-sh {
  color: var(--ft-sh);
}
.ft-sql {
  color: var(--ft-sql);
}
.ft-docker {
  color: var(--ft-docker);
}
.ft-git {
  color: var(--ft-git);
}
.ft-npm {
  color: var(--ft-npm);
}
.ft-claude {
  color: var(--ft-claude);
}
.ft-toml {
  color: var(--ft-toml);
}
</style>
