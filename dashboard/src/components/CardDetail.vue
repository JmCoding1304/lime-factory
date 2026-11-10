<script setup lang="ts">
// The card detail tab body. Mounted by RunView (267). All of its data lives in
// `lib/cardDetail.ts`; this file only wires the props, the 1 s clock, the
// More/Less toggle and the session-id copy. Tabs, Esc and Back live in RunView.

import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import type { FeedEvent, FeedRun } from "../lib/types";
import { cardDetail, type Artifact } from "../lib/cardDetail";
import { feedRows } from "../lib/runLine";

const props = defineProps<{
  run: FeedRun | null;
  events: FeedEvent[];
  phaseId: string | null;
}>();

// Row 63: an artifact that lives in the repo opens in the editor; the shell
// (RunView → Editor) owns the tab, so this only names the path.
const emit = defineEmits<{ open: [path: string] }>();

function openArtifact(a: Artifact) {
  if (a.open) emit("open", a.open);
}

const nowMs = ref(Date.now());

// Kept apart from `detail` so the 1 s tick does not re-serialize every event.
const rows = computed(() =>
  props.run ? feedRows(props.run.run.run_id, props.events) : [],
);

const detail = computed(() =>
  props.run ? cardDetail(props.run, rows.value, props.phaseId, nowMs.value) : null,
);

const more = ref(false);
const copied = ref(false);
const clipped = ref(false);

// The summary element, measured after render to learn whether the five-line
// clamp actually cut text. The clamp is applied unconditionally while
// collapsed, so the limit holds for any summary; `clipped` only decides
// whether More is offered, from the rendered line count rather than a
// character or newline heuristic.
const summaryEl = ref<HTMLElement | null>(null);

function measureClip() {
  const el = summaryEl.value;
  if (!el || more.value) return;
  clipped.value = el.scrollHeight - el.clientHeight > 1;
}

// Watch the phase id and the summary value as separate sources. A single
// getter returning an array would hand back a fresh array on every recompute,
// and the 1 s clock recomputes `detail` every tick even when neither value
// changed, which would reset More every second. Separate getters compare each
// source by value, so the reset fires only when phase or content really moves.
watch(
  [() => props.phaseId, () => detail.value?.outcome?.summary],
  async () => {
    more.value = false;
    copied.value = false;
    clipped.value = false;
    await nextTick();
    measureClip();
  },
  { immediate: true },
);

// A missing or denied clipboard write is swallowed; the footer stays as it was.
function copySession() {
  const id = detail.value?.sessionId;
  if (!id) return;
  Promise.resolve()
    .then(() => navigator.clipboard.writeText(id))
    .then(
      () => {
        copied.value = true;
      },
      () => {},
    );
}

let tick: ReturnType<typeof setInterval> | null = null;
onMounted(() => {
  tick = setInterval(() => (nowMs.value = Date.now()), 1000);
  window.addEventListener("resize", measureClip);
  measureClip();
});
onUnmounted(() => {
  if (tick !== null) clearInterval(tick);
  window.removeEventListener("resize", measureClip);
});
</script>

<template>
  <section v-if="detail" class="card-detail" role="region" :aria-label="detail.name">
    <div class="cd-body">
      <p class="cd-state">
        <span class="cd-word" :class="detail.tone">{{ detail.word }}</span>
        <span class="cd-attempt"> · attempt {{ detail.attempt }}</span>
      </p>

      <p v-if="detail.description" class="cd-desc">{{ detail.description }}</p>

      <dl class="cd-facts">
        <template v-for="f in detail.facts" :key="f.key">
          <dt>{{ f.label }}</dt>
          <dd :title="f.title">
            <span
              v-if="f.key === 'context' && detail.context"
              class="cd-bar"
            ><i
              :class="{ hot: detail.context.hot }"
              :style="{ width: detail.context.pct + '%' }"
            ></i></span>{{ f.text }}
          </dd>
        </template>
      </dl>

      <ul v-if="detail.signals.length" class="cd-signals">
        <li v-for="(s, i) in detail.signals" :key="i" :class="s.tone">
          <span class="cd-glyph">{{ s.glyph }}</span> {{ s.text }}
        </li>
      </ul>

      <div v-if="detail.error" class="cd-error">{{ detail.error }}</div>

      <p v-if="detail.live" class="cd-live">{{ detail.live }}</p>

      <section v-if="detail.outcome" class="cd-sec cd-outcome">
        <h4>
          Outcome
          <span class="cd-env" :class="detail.outcome.valid ? 'ok' : 'bad'">
            {{ detail.outcome.type }}<template v-if="detail.outcome.status"> · {{ detail.outcome.status }}</template>
          </span>
        </h4>
        <p
          v-if="detail.outcome.summary"
          ref="summaryEl"
          class="cd-summary"
          :class="{ clamp: !more }"
        >{{ detail.outcome.summary }}</p>
        <button
          v-if="clipped"
          type="button"
          class="cd-more"
          @click="more = !more"
        >{{ more ? 'Less' : 'More' }}</button>
        <details v-if="detail.outcome.notes" class="cd-notes">
          <summary>Notes for the next phase</summary>
          <p>{{ detail.outcome.notes }}</p>
        </details>
        <div v-if="detail.outcome.artifacts.length" class="cd-artifacts">
          <template v-for="a in detail.outcome.artifacts" :key="a.path">
            <button
              v-if="a.open"
              type="button"
              class="cd-file open"
              :title="'open ' + a.open"
              @click="openArtifact(a)"
            >{{ a.path }}</button>
            <code v-else class="cd-file">{{ a.path }}</code>
          </template>
        </div>
      </section>

      <details v-if="detail.touched" class="cd-sec cd-touched">
        <summary>
          {{ detail.touched.label }}<span
            v-if="detail.touched.diff"
            class="cd-diff"
            :class="{ bad: detail.touched.diff.over }"
          > · {{ detail.touched.diff.text }}</span>
        </summary>
        <ul class="cd-paths">
          <li v-for="p in detail.touched.paths" :key="p"><code>{{ p }}</code></li>
        </ul>
      </details>

      <section v-if="detail.checks.length" class="cd-sec cd-checks">
        <h4>
          Checks
          <span class="cd-count">
            {{ detail.checks.filter((c) => c.ok).length }}/{{ detail.checks.length }} passed
          </span>
        </h4>
        <!-- Rows 57/58/59: a passed gate stays collapsed behind its check
             count; a failed one opens with its violations and sums the
             passing evidence as "N other checks passed". -->
        <details
          v-for="c in detail.checks"
          :key="c.key"
          class="cd-check"
          :class="{ bad: !c.ok }"
          :open="!c.ok"
        >
          <summary>
            <span class="cd-mark">{{ c.ok ? '✓' : '✕' }}</span>
            <span class="cd-check-name">{{ c.name }}</span>
            <span v-if="detail.multi" class="cd-check-attempt"> · attempt {{ c.attempt }}</span>
            <span v-if="c.count" class="cd-count"> · {{ c.count }}</span>
          </summary>
          <ul v-if="c.violations.length" class="cd-viol">
            <li v-for="(v, i) in c.violations" :key="i">{{ v }}</li>
          </ul>
          <ul v-if="c.checks.length" class="cd-check-items">
            <li v-for="(k, i) in c.checks" :key="i" :class="{ bad: !k.ok }">
              <span class="cd-mark">{{ k.ok ? '✓' : '✕' }}</span>
              <span class="cd-check-name">{{ k.item }}</span>
              <span v-if="k.note" class="cd-note"> · {{ k.note }}</span>
            </li>
          </ul>
        </details>
      </section>

      <details v-if="detail.request" class="cd-sec cd-request" open>
        <summary>Request</summary>
        <pre class="cd-pre">{{ detail.request }}</pre>
      </details>

      <details v-if="detail.brief" class="cd-sec cd-brief">
        <summary>Brief · the ticket text the planner got</summary>
        <pre class="cd-pre">{{ detail.brief }}</pre>
      </details>
    </div>

    <footer class="cd-foot">
      <code class="cd-phase" title="phase id">{{ detail.id }}</code>
      <button
        v-if="detail.sessionId"
        type="button"
        class="cd-sid"
        title="copy session id"
        @click="copySession"
      >
        <span>{{ copied ? 'copied' : 'session' }}</span>
        <code>{{ detail.sessionId }}</code>
      </button>
    </footer>
  </section>
</template>

<style scoped>
.card-detail {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
  font-size: 13px;
}

.cd-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 12px 14px;
  overflow: auto;
  min-height: 0;
}

.cd-state {
  margin: 0;
  display: flex;
  align-items: baseline;
  gap: 2px;
}

.cd-word {
  font-weight: 600;
}

.cd-word.warn {
  color: var(--warn);
}

.cd-word.bad {
  color: var(--bad);
}

.cd-word.ok {
  color: var(--ok);
}

.cd-attempt,
.cd-check-attempt,
.cd-count {
  color: var(--dim);
  font-family: var(--mono);
  font-size: 12px;
}

.cd-desc {
  margin: 0;
  color: var(--dim);
  overflow-wrap: anywhere;
}

.cd-facts {
  margin: 0;
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 12px;
}

.cd-facts dt {
  color: var(--faint);
  font-size: 12px;
}

.cd-facts dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.cd-bar {
  display: inline-block;
  vertical-align: middle;
  width: 56px;
  height: 6px;
  margin-right: 6px;
  border-radius: 3px;
  background: var(--faint);
  overflow: hidden;
}

.cd-bar i {
  display: block;
  height: 100%;
  background: var(--ok);
}

.cd-bar i.hot {
  background: var(--warn);
}

.cd-signals {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.cd-signals li {
  overflow-wrap: anywhere;
}

.cd-signals li.warn {
  color: var(--warn);
}

.cd-signals li.bad {
  color: var(--bad);
}

.cd-glyph {
  font-family: var(--mono);
}

.cd-error {
  padding: 9px 13px;
  border: 1px solid var(--bad);
  background: var(--bad-chip);
  border-radius: 8px;
  font-size: 12.5px;
  color: var(--bad);
  font-family: var(--mono);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.cd-live {
  margin: 0;
  color: var(--run);
  font-family: var(--mono);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.cd-sec {
  border-top: 1px solid var(--faint);
  padding-top: 10px;
}

.cd-sec h4 {
  margin: 0 0 6px;
  font-size: 12px;
  font-weight: 700;
  display: flex;
  align-items: baseline;
  gap: 8px;
  color: var(--dim);
}

.cd-env {
  font-family: var(--mono);
  font-size: 11.5px;
  font-weight: 500;
}

.cd-env.ok {
  color: var(--ok);
}

.cd-env.bad {
  color: var(--bad);
}

.cd-summary {
  margin: 0 0 6px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.cd-summary.clamp {
  display: -webkit-box;
  -webkit-line-clamp: 5;
  line-clamp: 5;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.cd-more {
  margin-bottom: 6px;
  padding: 2px 8px;
  border: 1px solid var(--faint);
  background: transparent;
  color: var(--dim);
  border-radius: 6px;
  font-size: 11.5px;
  cursor: pointer;
}

.cd-notes summary,
.cd-touched summary,
.cd-request summary,
.cd-brief summary,
.cd-check summary {
  cursor: pointer;
  color: var(--dim);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.cd-notes p {
  margin: 6px 0 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.cd-artifacts {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

.cd-file {
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--dim);
  overflow-wrap: anywhere;
}

/* Row 63: a repo artifact is a chip that opens; one outside the repo is text. */
.cd-file.open {
  padding: 1px 7px;
  border: 1px solid var(--faint);
  background: transparent;
  border-radius: 6px;
  cursor: pointer;
}

.cd-file.open:hover {
  color: var(--text);
  border-color: var(--run);
}

.cd-diff {
  color: var(--dim);
  font-family: var(--mono);
  font-size: 11.5px;
}

.cd-diff.bad {
  color: var(--bad);
}

.cd-paths {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.cd-paths code {
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--dim);
  overflow-wrap: anywhere;
}

.cd-check {
  padding: 2px 0;
  overflow-wrap: anywhere;
}

.cd-check.bad > summary {
  color: var(--bad);
}

.cd-check-items {
  list-style: none;
  margin: 4px 0 0 18px;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 11.5px;
  color: var(--dim);
}

.cd-check-items > li {
  overflow-wrap: anywhere;
}

.cd-check-items > li.bad {
  color: var(--bad);
}

.cd-mark {
  font-family: var(--mono);
  color: var(--ok);
}

.cd-check.bad > summary .cd-mark,
.cd-check-items > li.bad .cd-mark {
  color: var(--bad);
}

.cd-note {
  color: var(--faint);
}

.cd-check-name {
  font-family: var(--mono);
  font-size: 12px;
}

.cd-viol {
  list-style: none;
  margin: 4px 0 0 18px;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  color: var(--bad);
  font-family: var(--mono);
  font-size: 11.5px;
  overflow-wrap: anywhere;
}

.cd-pre {
  margin: 6px 0 0;
  padding: 8px 10px;
  border: 1px solid var(--faint);
  border-radius: 6px;
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--dim);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.cd-foot {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px;
  border-top: 1px solid var(--faint);
  flex: none;
}

.cd-phase {
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--faint);
}

.cd-sid {
  display: flex;
  align-items: baseline;
  gap: 6px;
  padding: 2px 8px;
  border: 1px solid var(--faint);
  background: transparent;
  color: var(--dim);
  border-radius: 6px;
  font-size: 11.5px;
  cursor: pointer;
  min-width: 0;
}

.cd-sid code {
  font-family: var(--mono);
  color: var(--dim);
  overflow-wrap: anywhere;
}
</style>
