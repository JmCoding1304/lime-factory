<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import type { SessionCost } from '../lib/types'
import { isCashModel } from '../lib/types'
import { fetchCost } from '../lib/api'
import { modelColor, modelLogo, modelName } from '../lib/models'
import { fmtMs } from '../lib/format'
import { fmtUsd, rankModels, rankUsage, rankSpeed, bySpeed, roleCards } from '../lib/cost'
import type { ModelStat, RoleCard } from '../lib/cost'
import { CHART, BASE_Y, ICON_PX, ICON_GAP, LABEL_ROT, TABS, DEFAULT_TAB, FOOTNOTE, NOTIONAL_LABEL, NOTIONAL_SUB, bars, chartMax, chartHeight, padBottom, slotWidth, yOf, yLabel } from '../lib/costPage'
import type { TabId } from '../lib/costPage'

const props = defineProps<{ project: string }>()

const notionalOn = ref(true)
const days = ref(7)
const rows = shallowRef<SessionCost[]>([])
const loaded = ref(false)
const error = ref<string | null>(null)
const tab = ref<TabId>(DEFAULT_TAB)

let timer: ReturnType<typeof setInterval> | undefined

async function load() {
  try {
    rows.value = await fetchCost(days.value)
    error.value = null
    loaded.value = true
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

onMounted(() => {
  void load()
  timer = setInterval(() => void load(), 60_000)
})

watch(days, () => void load())

onUnmounted(() => {
  clearInterval(timer)
})

const visible = computed(() =>
  props.project === 'all' ? rows.value : rows.value.filter((r) => r.project === props.project),
)

const ranked = computed(() => {
  if (tab.value === 'usage') return rankUsage(visible.value, notionalOn.value)
  if (tab.value === 'cost') return rankModels(visible.value, notionalOn.value)
  return rankSpeed(visible.value, notionalOn.value)
})

const cards = computed(() => roleCards(visible.value, notionalOn.value))

const hasSub = computed(() => visible.value.some((r) => !isCashModel(r.model)))

// ── chart geometry ──────────────────────────────────────────────────────────

const currentTab = computed(() => TABS.find((t) => t.id === tab.value))

const slot = computed(() => slotWidth(ranked.value.length))

const barW = computed(() =>
  tab.value === 'speed' ? Math.min(64, slot.value * 0.4) : Math.min(88, slot.value * 0.55),
)

function xMid(i: number) {
  return CHART.PAD.l + slot.value * (i + 0.5)
}

const padB = computed(() => padBottom(ranked.value.map((m) => m.model)))

const H = computed(() => chartHeight(padB.value))

const chartBars = computed(() => bars(tab.value, ranked.value, slot.value))

const max = computed(() => chartMax(tab.value, chartBars.value))

const y = computed(() => yOf(max.value))

const grid = computed(() => [1, 2, 3, 4].map((g) => (max.value / 4) * g))

const caption = computed(() => currentTab.value?.caption ?? '')
const aria = computed(() => currentTab.value?.aria ?? '')

// ── display helpers ─────────────────────────────────────────────────────────

const titleFor = (n: number | null) => (n !== null && n < 0.01 ? String(n) : undefined)

function rowStats(g: RoleCard): ModelStat[] {
  return tab.value === 'speed' ? bySpeed(g.stats) : g.stats
}

function rowValue(s: ModelStat): string {
  if (tab.value === 'usage') {
    return s.priced === null ? 'n/a' : fmtUsd(s.priced)
  }
  if (tab.value === 'cost') {
    if (s.priced === null) return 'n/a'
    return s.perWin === null ? '0 wins' : fmtUsd(s.perWin)
  }
  // speed
  return s.medianMs === null ? 'n/a' : fmtMs(s.medianMs)
}

function rowTitle(s: ModelStat): string | undefined {
  if (tab.value === 'speed') return undefined
  const n = tab.value === 'usage' ? s.priced : s.perWin
  return titleFor(n)
}

function bestOf(g: RoleCard): string | null {
  return tab.value === 'speed' ? g.fastest : g.best
}

const bestLabel = computed(() => (tab.value === 'speed' ? 'fastest' : 'best value'))

function excluded(s: ModelStat): boolean {
  return tab.value !== 'speed' && s.sub && !notionalOn.value
}
</script>

<template>
  <div class="cost-page">
    <div v-if="error" class="error-bar">cost fetch failed — {{ error }}</div>

    <header class="head">
      <div>
        <h1>Cost</h1>
        <p class="q">which model is worth it? — $ per successful run, not per token</p>
      </div>
      <div class="head-controls">
        <div class="chips tabs" role="tablist">
          <button
            v-for="t in TABS"
            :key="t.id"
            role="tab"
            :aria-selected="tab === t.id"
            :class="{ on: tab === t.id }"
            @click="tab = t.id"
          >{{ t.label }}</button>
        </div>
        <span class="vdiv" />
        <div class="chips">
          <button v-for="d in [7, 30, 90]" :key="d" :class="{ on: days === d }" @click="days = d">{{ d }}d</button>
        </div>
        <label class="switch" :class="{ on: notionalOn }">
          <button class="track" role="switch" :aria-checked="notionalOn" @click="notionalOn = !notionalOn">
            <span class="knob" />
          </button>
          <span class="sw-label">
            {{ NOTIONAL_LABEL }}
            <small>{{ notionalOn ? NOTIONAL_SUB.on : NOTIONAL_SUB.off }}</small>
          </span>
        </label>
      </div>
    </header>

    <div v-if="loaded && !visible.length" class="empty">no finished runs in the last {{ days }} days</div>

    <template v-else>
      <div class="chart-card">
        <h5>{{ caption }}</h5>
        <svg :viewBox="`0 0 ${CHART.W} ${H}`" class="chart" role="img" :aria-label="aria">
          <defs>
            <pattern
              v-for="(b, i) in chartBars"
              :key="`h-${i}`"
              :id="`hatch-${i}`"
              width="6"
              height="6"
              patternTransform="rotate(45)"
              patternUnits="userSpaceOnUse"
            >
              <line x1="0" y1="0" x2="0" y2="6" class="hatch-line" :style="{ stroke: modelColor(b.model) }" />
            </pattern>
          </defs>
          <line
            v-for="(gv, g) in grid"
            :key="g"
            :x1="CHART.PAD.l"
            :x2="CHART.W - CHART.PAD.r"
            :y1="y(gv)"
            :y2="y(gv)"
            class="grid"
          />
          <text
            v-for="(gv, g) in grid"
            :key="g"
            :x="CHART.PAD.l - 8"
            :y="y(gv) + 4"
            class="glbl"
          >
            <title v-if="titleFor(gv) && tab !== 'speed'">{{ titleFor(gv) }}</title>
            {{ yLabel(tab, gv) }}
          </text>

          <g v-for="(b, i) in chartBars" :key="b.model">
            <title>{{ b.title }}</title>
            <!-- dashed ghost bar when value is null -->
            <g v-if="b.value === null">
              <rect
                :x="xMid(i) - barW / 2"
                :y="y(max * 0.98)"
                :width="barW"
                :height="BASE_Y - y(max * 0.98)"
                class="ghost"
                rx="4"
              />
              <text :x="xMid(i)" :y="y(max * 0.5)" class="ghost-lbl">{{ b.ghost }}</text>
            </g>
            <template v-else>
              <!-- solid bar in model colour -->
              <rect
                :x="xMid(i) - barW / 2"
                :y="y(b.value)"
                :width="barW"
                :height="BASE_Y - y(b.value)"
                :fill="modelColor(b.model)"
                rx="4"
              />
              <!-- hatch overlay on notional segment -->
              <rect
                v-if="b.solid < b.value"
                :x="xMid(i) - barW / 2"
                :y="y(b.value)"
                :width="barW"
                :height="y(b.solid) - y(b.value)"
                :fill="`url(#hatch-${i})`"
                rx="4"
              />
              <!-- whisker for speed tab -->
              <template v-if="b.whisker">
                <line
                  :x1="xMid(i)" :x2="xMid(i)"
                  :y1="y(b.whisker.max)" :y2="y(b.whisker.min)"
                  class="whisk" :style="{ stroke: modelColor(b.model) }"
                />
                <line
                  :x1="xMid(i) - 8" :x2="xMid(i) + 8"
                  :y1="y(b.whisker.max)" :y2="y(b.whisker.max)"
                  class="whisk" :style="{ stroke: modelColor(b.model) }"
                />
                <line
                  :x1="xMid(i) - 8" :x2="xMid(i) + 8"
                  :y1="y(b.whisker.min)" :y2="y(b.whisker.min)"
                  class="whisk" :style="{ stroke: modelColor(b.model) }"
                />
              </template>
              <!-- value text: inside bar if tall enough, above otherwise -->
              <text
                :x="xMid(i)"
                :y="BASE_Y - y(b.value) >= 20 ? y(b.value) + 14 : y(b.value) - 6"
                :class="BASE_Y - y(b.value) >= 20 ? 'val-in' : 'val-out'"
              >{{ b.label }}</text>
            </template>
            <!-- provider icon centred under the bar base -->
            <g
              class="xicon"
              :transform="`translate(${xMid(i) - ICON_PX / 2}, ${BASE_Y + ICON_GAP})`"
              :style="{ color: modelColor(b.model) }"
              v-html="modelLogo(b.model)"
            />
            <!-- model name hung down-left rotated from the icon -->
            <text
              :transform="`translate(${xMid(i)}, ${BASE_Y + ICON_GAP + ICON_PX - 2}) rotate(${LABEL_ROT})`"
              class="xlbl-rot"
            >{{ modelName(b.model) }}</text>
          </g>
        </svg>
        <p v-if="tab === 'speed' || hasSub" class="footnote mono">{{ FOOTNOTE[tab] }}</p>
      </div>

      <div class="roles">
        <div v-for="g in cards" :key="g.role" class="role-card">
          <h5>{{ g.role }}</h5>
          <div
            v-for="s in rowStats(g)"
            :key="s.model"
            class="rrow"
            :class="{ best: s.model === bestOf(g), excluded: excluded(s) }"
          >
            <span class="role-logo" :style="{ color: modelColor(s.model) }" v-html="modelLogo(s.model)" />
            <span class="rname" :title="s.model">{{ modelName(s.model) }}</span>
            <span class="dag">{{ s.sub ? '†' : '' }}</span>
            <span class="rnum mono">{{ Math.round(s.successRate * 100) }}%</span>
            <span class="rnum mono usd" :title="rowTitle(s)">{{ rowValue(s) }}</span>
            <span class="best-tag">{{ s.model === bestOf(g) ? bestLabel : '' }}</span>
          </div>
          <div v-if="!g.stats.length" class="faint" style="font-size: 12px">no sessions in window</div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.cost-page {
  padding: 28px 24px 80px;
  max-width: 1060px;
}

.head {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 24px;
  flex-wrap: wrap;
  margin-bottom: 18px;
}

.head-controls {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}

h1 {
  margin: 0;
  font-size: 26px;
  font-weight: 700;
  letter-spacing: -0.01em;
}

.q {
  margin: 4px 0 0;
  color: var(--faint);
  font-size: 13.5px;
}

.chips {
  display: flex;
  gap: 4px;
}

.chips button {
  padding: 5px 12px;
  border-radius: 999px;
  border: 1px solid var(--border);
  color: var(--dim);
  font-size: 12px;
  font-family: var(--mono);
  display: inline-flex;
  align-items: center;
}

.chips button.on {
  background: var(--surface-2);
  border-color: var(--border-strong);
  color: var(--text);
  font-weight: 500;
}

/* tab pills get a lime dot so they read as tabs, not window chips */
.tabs button.on {
  border-color: var(--lime);
}

.tabs button.on::before {
  content: "";
  width: 6px;
  height: 6px;
  border-radius: 2px;
  background: var(--lime);
  margin-right: 6px;
}

.vdiv {
  width: 1px;
  height: 20px;
  background: var(--border-strong);
  flex: none;
}

.switch {
  display: flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
}

.track {
  width: 38px;
  height: 22px;
  border-radius: 999px;
  background: var(--surface-2);
  border: 1px solid var(--border-strong);
  position: relative;
  flex: none;
  transition: background 0.2s;
}

.switch.on .track {
  background: var(--lime);
  border-color: var(--lime);
}

.knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--surface);
  box-shadow: var(--shadow);
  transition: left 0.2s;
}

.switch.on .knob {
  left: 18px;
}

.sw-label {
  font-size: 12.5px;
  font-weight: 500;
  line-height: 1.3;
  width: 230px;
  white-space: nowrap;
}

.sw-label small {
  display: block;
  font-weight: 400;
  color: var(--faint);
  font-size: 11px;
}

.chart-card {
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  box-shadow: var(--shadow);
  padding: 14px 16px 10px;
}

.chart-card h5,
.role-card h5 {
  margin: 0 0 8px;
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.09em;
  color: var(--faint);
  font-weight: 500;
}

.chart {
  width: 100%;
  height: auto;
  display: block;
}

.grid {
  stroke: var(--border);
  stroke-width: 1;
}

.glbl {
  font-family: var(--mono);
  font-size: 10px;
  fill: var(--faint);
  text-anchor: end;
}

.hatch-line {
  stroke-width: 2;
  opacity: 0.55;
}

.whisk {
  stroke-width: 1.5;
  opacity: 0.85;
}

.ghost {
  fill: none;
  stroke: var(--border-strong);
  stroke-dasharray: 4 3;
}

.ghost-lbl {
  font-family: var(--mono);
  font-size: 10px;
  fill: var(--faint);
  text-anchor: middle;
}

/* value printed inside the bar, white mono — AA-style */
.val-in {
  font-family: var(--mono);
  font-size: 11px;
  fill: #fff;
  text-anchor: middle;
  font-weight: 500;
  pointer-events: none;
}

/* value printed above the bar when the bar is too short */
.val-out {
  font-family: var(--mono);
  font-size: 11px;
  fill: var(--text);
  text-anchor: middle;
  font-weight: 500;
  pointer-events: none;
}

/* model name tilted -55°, anchored at its end under the icon */
.xlbl-rot {
  font-family: var(--mono);
  font-size: 10.5px;
  fill: var(--dim);
  text-anchor: end;
}

/* 18px provider icon centred under the bar base */
.xicon :deep(svg) {
  width: 18px;
  height: 18px;
  display: block;
}

.footnote {
  margin: 6px 0 2px;
  font-size: 10.5px;
  color: var(--faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.roles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: 14px;
  margin-top: 16px;
}

.role-card {
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  box-shadow: var(--shadow);
  padding: 12px 14px;
}

.rrow {
  display: grid;
  grid-template-columns: 26px minmax(0, 1fr) 12px 40px 56px 64px;
  column-gap: 8px;
  align-items: center;
  height: 32px;
  padding: 0 8px;
  border-radius: 7px;
  font-size: 12.5px;
}

.rrow + .rrow {
  margin-top: 2px;
}

.rrow.best {
  background: var(--ok-chip);
}

.rrow.excluded {
  opacity: 0.55;
}

.rname {
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.dag {
  color: var(--faint);
  text-align: center;
}

.rnum {
  text-align: right;
  font-size: 11.5px;
  color: var(--dim);
}

.rnum.usd {
  color: var(--text);
}

.best-tag {
  font-size: 10px;
  font-weight: 500;
  color: var(--ok);
  white-space: nowrap;
  text-align: right;
}
</style>